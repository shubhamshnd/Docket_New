"""Platform administration: people & hierarchy (super admin), app configuration (super admin or app admin)."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import or_

from core import approvals, sync
from core.access import can
from core.db import db
from core.models import AppGrant, ApprovalNode, Employee, Site, departments as all_departments
from core.registry import APPS, get_setting, set_setting
from core.security import superadmin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")
SUBJECT_TYPES = {"all": "Everyone", "department": "Department", "employee": "Employee"}
PAGE_SIZE = 50


def _emp_or_none(emp_id):
    emp_id = (emp_id or "").strip()
    return db.session.get(Employee, emp_id) if emp_id else None


@bp.get("/")
def index():
    if not g.user.is_superadmin and not any(can(g.user, a, "admin") for a in APPS):
        abort(403)
    base = Employee.query.filter_by(status="Active")
    if g.get("site_id"):
        base = base.filter(Employee.site_id == g.site_id)
    stats = {
        "employees": base.count(),
        "departments": len(all_departments(site_id=g.get("site_id"))),
        "no_manager": base.filter(Employee.reporting_manager_id.is_(None)).count(),
        "no_email": base.filter(Employee.email.is_(None)).count(),
    }
    return render_template("admin/index.html", stats=stats)


# ---------------------------------------------------------------- employees
@bp.get("/employees")
@superadmin_required
def employees():
    from core.models import Site
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "Active")
    source = request.args.get("source", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    query = Employee.query

    site_param = request.args.get("site")
    if site_param == "all":
        selected_site = "all"
    elif site_param == "unassigned":
        selected_site = "unassigned"
        query = query.filter(Employee.site_id.is_(None))
    elif site_param and site_param.isdigit():
        selected_site = int(site_param)
        query = query.filter(Employee.site_id == selected_site)
    elif g.get("site_id"):
        selected_site = g.site_id
        query = query.filter(Employee.site_id == g.site_id)
    else:
        selected_site = "all"

    if status:
        query = query.filter(Employee.status == status)
    if source:
        query = query.filter(Employee.source == source)
    if request.args.get("dept"):
        query = query.filter(Employee.department == request.args["dept"])
    if request.args.get("no_manager"):
        query = query.filter(Employee.reporting_manager_id.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Employee.name.ilike(like), Employee.employee_id.ilike(like),
                                 Employee.designation.ilike(like), Employee.email.ilike(like)))
    total = query.count()
    people = query.order_by(Employee.name).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    sites = Site.query.filter_by(is_active=True).order_by(Site.name).all()
    unassigned_count = Employee.query.filter(Employee.site_id.is_(None)).count()
    dept_site = selected_site if isinstance(selected_site, int) else None
    return render_template("admin/employees.html", people=people, total=total, page=page, page_size=PAGE_SIZE,
                           departments=all_departments(site_id=dept_site), source=source, sites=sites, selected_site=selected_site,
                           unassigned_count=unassigned_count)


@bp.post("/employees/bulk-assign-site")
@superadmin_required
def bulk_assign_site():
    site_id = request.form.get("target_site_id", type=int)
    emp_ids = request.form.getlist("emp_ids")
    if site_id:
        if request.form.get("assign_unassigned"):
            count = Employee.query.filter(Employee.site_id.is_(None)).update({"site_id": site_id}, synchronize_session=False)
            db.session.commit()
            flash(f"Assigned {count} unassigned employees to selected site.", "success")
        elif emp_ids:
            count = Employee.query.filter(Employee.employee_id.in_(emp_ids)).update({"site_id": site_id}, synchronize_session=False)
            db.session.commit()
            flash(f"Assigned {count} employees to selected site.", "success")
    return redirect(request.referrer or url_for("admin.employees"))


@bp.route("/employees/new", methods=["GET", "POST"])
@bp.route("/employees/<emp_id>", methods=["GET", "POST"])
@superadmin_required
def employee_edit(emp_id=None):
    emp = db.session.get(Employee, emp_id) if emp_id else None
    if emp_id and emp is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        if emp is None:
            new_id = f.get("employee_id", "").strip()
            if not new_id or db.session.get(Employee, new_id):
                flash("Employee ID is missing or already exists.", "error")
                return redirect(request.url)
            emp = Employee(employee_id=new_id, source="manual")
            db.session.add(emp)
        else:
            emp.source = "manual"  # manual edits are preserved across future syncs
        for field in ("name", "designation", "department", "phone", "gender", "category", "status", "company_name"):
            setattr(emp, field, f.get(field, "").strip() or None)
        emp.name = emp.name or emp.employee_id
        emp.status = emp.status or "Active"
        emp.email = f.get("email", "").strip().lower() or None
        site_id_val = f.get("site_id", type=int)
        emp.site_id = site_id_val if site_id_val else None
        manager = _emp_or_none(f.get("manager_id"))
        if f.get("manager_id") and manager is None:
            flash("Reporting manager not found.", "error")
            return redirect(request.url)
        if manager and _creates_cycle(emp, manager):
            flash("That manager reports (directly or indirectly) to this employee.", "error")
            return redirect(request.url)
        if (manager.employee_id if manager else None) != emp.reporting_manager_id:
            emp.manager_locked = True  # hand-edited: keep it across syncs
        emp.reporting_manager_id = manager.employee_id if manager else None
        if f.get("unlock_manager"):
            emp.manager_locked = False
        emp.is_superadmin = bool(f.get("is_superadmin")) if emp.employee_id != g.user.employee_id else emp.is_superadmin
        if f.get("password"):
            emp.set_password(f["password"])
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("admin.employee_edit", emp_id=emp.employee_id))
    from core.models import Site
    return render_template("admin/employee_edit.html", emp=emp,
                           departments=all_departments(), sites=Site.query.filter_by(is_active=True).order_by(Site.name).all())


def _creates_cycle(emp, manager):
    seen, cur = set(), manager
    while cur is not None and cur.employee_id not in seen:
        if cur.employee_id == emp.employee_id:
            return True
        seen.add(cur.employee_id)
        cur = cur.manager
    return False


@bp.post("/employees/<emp_id>/impersonate")
@superadmin_required
def impersonate(emp_id):
    """Sign in as someone to test their view. The banner offers a way back."""
    emp = db.session.get(Employee, emp_id) or abort(404)
    admin_id = g.user.employee_id
    current_app.logger.warning("%s is impersonating %s", admin_id, emp_id)
    session.clear()
    session.update(emp_id=emp.employee_id, impersonator=admin_id)
    return redirect(url_for("main.home"))


def _get_odbc():
    odbc = (current_app.config.get("SOURCE_ODBC") or "").strip("\"' ")
    if not odbc or ("SERVER=" not in odbc.upper() and "DSN=" not in odbc.upper()):
        from dotenv import dotenv_values
        from pathlib import Path
        env = dotenv_values(Path(current_app.root_path).parent / ".env")
        odbc = (env.get("SOURCE_ODBC") or "").strip("\"' ")
        if not odbc or ("SERVER=" not in odbc.upper() and "DSN=" not in odbc.upper()):
            if env.get("SOURCE_DB_SERVER"):
                server = env.get("SOURCE_DB_SERVER")
                port = env.get("SOURCE_DB_PORT", "1433")
                driver = env.get("SOURCE_DB_DRIVER", "SQL Server")
                db_name = env.get("SOURCE_DB_NAME", "")
                user = env.get("SOURCE_DB_USER", "")
                pwd = env.get("SOURCE_DB_PASSWORD", "")
                server_str = f"{server},{port}" if port else server
                parts = [f"DRIVER={{{driver}}}", f"SERVER={server_str}", f"DATABASE={db_name}"]
                if str(env.get("SOURCE_DB_TRUSTED_CONNECTION", "")).lower() in ("1", "true", "yes"):
                    parts.append("Trusted_Connection=yes")
                else:
                    if user:
                        parts.append(f"UID={user}")
                    if pwd:
                        parts.append(f"PWD={pwd}")
                odbc = ";".join(parts)
        if odbc and ("SERVER=" in odbc.upper() or "DSN=" in odbc.upper()):
            current_app.config["SOURCE_ODBC"] = odbc
    return odbc or ""


# ---------------------------------------------------------------- sync
VIEW_LABELS = {
    "view_EmployeeMaster_Report_Staff": "Employees (Staff)",
    "view_EmployeeMaster_Report_Associates": "Associates (Contractors)",
}


@bp.route("/sync", methods=["GET", "POST"])
@superadmin_required
def sync_page():
    result = None
    odbc = _get_odbc()
    saved_cfg = sync.get_sync_config()
    if request.method == "POST":
        deactivate = bool(request.form.get("deactivate_missing"))
        deactivate_unselected = bool(request.form.get("deactivate_unselected"))
        target_site_id = request.form.get("target_site_id", type=int) or g.get("site_id")
        try:
            if request.form.get("mode") == "file":
                upload = request.files.get("file")
                if not upload or not upload.filename:
                    raise ValueError("Choose a file to upload.")
                header, rows = sync.read_file(upload.filename, upload.stream)
                records = sync.normalize(header, rows, "upload")
                result = sync.upsert(records, default_site_id=target_site_id, deactivate_missing=deactivate)
            else:
                if not odbc:
                    raise ValueError("SOURCE_ODBC is not configured in .env.")
                views = request.form.getlist("views")
                if not views:
                    raise ValueError("Please select at least one view to sync (e.g. Employees).")
                active_only = bool(request.form.get("active_only"))

                # Persist sync choices so they remain the same until explicitly changed
                sync.save_sync_config(
                    views=views,
                    active_only=active_only,
                    deactivate_missing=deactivate,
                    deactivate_unselected=True,
                )
                saved_cfg = sync.get_sync_config()

                # Fetch company mapping from Mantra view_Transaction_Report
                company_map = sync.fetch_company_mapping(odbc)

                records = []
                for view in views:
                    header, rows = sync.fetch_view(odbc, view, active_only)
                    records += sync.normalize(header, rows, current_app.config["SOURCE_VIEWS"].get(view, view))
                # staff first so it wins inside one run as well
                records.sort(key=lambda r: sync.SOURCE_PRIORITY.get(r["source"], 9), reverse=True)
                result = sync.upsert(
                    records,
                    company_map=company_map,
                    default_site_id=target_site_id,
                    deactivate_missing=deactivate,
                    deactivate_unselected=True,
                )
            db.session.commit()
            total_kept = result.get("kept_manual", 0) + result.get("kept_staff", 0)
            msg = (
                f"Sync completed! Processed {result['received']} rows: "
                f"{result['inserted']} added, {result['updated']} updated, "
                f"{total_kept} preserved"
            )
            if result.get("deactivated"):
                msg += f", {result['deactivated']} marked inactive"
            msg += "."
            flash(msg, "success")
        except Exception as exc:  # report any source/file problem to the admin
            db.session.rollback()
            current_app.logger.exception("Employee sync failed")
            flash(f"Sync failed: {exc}", "error")
    return render_template(
        "admin/sync.html",
        result=result,
        views=current_app.config["SOURCE_VIEWS"],
        view_labels=VIEW_LABELS,
        saved_views=saved_cfg.get("views", ["view_EmployeeMaster_Report_Staff"]),
        saved_active_only=saved_cfg.get("active_only", True),
        saved_deactivate_missing=saved_cfg.get("deactivate_missing", False),
        saved_deactivate_unselected=saved_cfg.get("deactivate_unselected", False),
        columns=sync.COLUMNS,
        odbc_ready=bool(odbc),
        sites=Site.query.filter_by(is_active=True).order_by(Site.name).all(),
        active_site_id=g.get("site_id"),
    )


# ---------------------------------------------------------------- app configuration
def _app_admin(code):
    app = APPS.get(code) or abort(404)
    if not can(g.user, code, "admin"):
        abort(403)
    return app


@bp.get("/apps")
def apps():
    mine = [a for a in APPS.values() if can(g.user, a.code, "admin")]
    if not mine:
        abort(403)
    return render_template("admin/apps.html", apps=mine)


@bp.route("/apps/<code>", methods=["GET", "POST"])
def app_config(code):
    app = _app_admin(code)
    tab = request.args.get("tab", "access")
    if request.method == "POST":
        handler = {"access": _save_grant, "approvals": _save_chart, "settings": _save_settings}.get(tab)
        if handler and handler(app) is not False:
            db.session.commit()
            flash("Configuration saved.", "success")
        return redirect(url_for("admin.app_config", code=code, tab=tab))

    grants = {}
    for gr in AppGrant.query.filter_by(app_code=code).all():
        grants.setdefault(gr.permission, []).append(gr)
    names = {e.employee_id: e.name for e in Employee.query.filter(Employee.employee_id.in_(
        [gr.subject_value for gs in grants.values() for gr in gs if gr.subject_type == "employee"])).all()}
    curr_site_id = g.get("site_id")
    nodes_q = ApprovalNode.query.filter_by(app_code=code)
    if curr_site_id:
        nodes_q = nodes_q.filter_by(site_id=curr_site_id)
    nodes = nodes_q.order_by(ApprovalNode.position, ApprovalNode.id).all()
    preview_emp = _emp_or_none(request.args.get("preview"))
    preview = approvals.plan(code, preview_emp, {k: True for k in app.conditions}) if preview_emp else None
    people_q = Employee.query.filter_by(status="Active")
    if curr_site_id:
        people_q = people_q.filter(Employee.site_id == curr_site_id)
    people = people_q.order_by(Employee.name).all()

    return render_template(
        "admin/app_config.html", app=app, tab=tab, grants=grants, names=names, subject_types=SUBJECT_TYPES,
        departments=all_departments(site_id=g.get("site_id")), settings={k: get_setting(code, k) for k in app.all_settings},
        roots=[n for n in nodes if n.parent_id is None], nodes=nodes, preview_emp=preview_emp, preview=preview,
        people=people,
    )


def _save_grant(app):
    f = request.form
    if f.get("remove"):
        grant = db.session.get(AppGrant, f.get("remove", type=int))
        if grant and grant.app_code == app.code:
            if grant.permission == "admin" and not g.user.is_superadmin:
                flash("Only a super admin can change application admins.", "error")
                return False
            db.session.delete(grant)
        return None
    perm, kind = f.get("permission"), f.get("subject_type")
    if perm not in app.all_permissions or kind not in SUBJECT_TYPES:
        abort(400)
    if perm == "admin" and not g.user.is_superadmin:
        flash("Only a super admin can change application admins.", "error")
        return False
    value = {"all": "", "department": f.get("department", ""), "employee": f.get("employee_id", "").strip()}[kind]
    if kind == "employee" and not db.session.get(Employee, value):
        flash("Employee not found.", "error")
        return False
    if kind != "all" and not value:
        flash("Choose who gets this permission.", "error")
        return False
    if not AppGrant.query.filter_by(app_code=app.code, permission=perm, subject_type=kind, subject_value=value).first():
        db.session.add(AppGrant(app_code=app.code, permission=perm, subject_type=kind, subject_value=value))


def _node(app, node_id):
    node = db.session.get(ApprovalNode, node_id or 0)
    if node is None or node.app_code != app.code:
        abort(404)
    return node


def _save_chart(app):
    """Org chart edits. In-flight requests keep their snapshotted approvers; new submissions use the new chart."""
    f = request.form
    action = f.get("action")
    curr_site_id = g.get("site_id")
    if action == "add":
        parent = _node(app, f.get("parent_id", type=int)) if f.get("parent_id") else None
        target_site_id = parent.site_id if parent else curr_site_id
        if parent:
            siblings = parent.children
        else:
            q = ApprovalNode.query.filter_by(app_code=app.code, parent_id=None)
            if target_site_id:
                q = q.filter_by(site_id=target_site_id)
            siblings = q.all()
        db.session.add(ApprovalNode(app_code=app.code, site_id=target_site_id, parent=parent,
                                    label=f.get("label", "").strip() or "New level",
                                    position=len(siblings)))
        return None
    node = _node(app, f.get("node_id", type=int))
    if action == "update":
        node.label = f.get("label", "").strip() or node.label
        cond = f.get("condition", "")
        node.condition = cond if cond in app.conditions else ""
        node.entry_departments = "\n".join(d for d in f.getlist("entry_departments") if d)
        node.entry_all = f.get("entry_all") == "1"
        new_parent = _node(app, f.get("parent_id", type=int)) if f.get("parent_id") else None
        probe = new_parent
        while probe is not None:  # can't move a box under itself
            if probe.id == node.id:
                flash("A level can't report to one of its own sub-levels.", "error")
                return False
            probe = probe.parent
        node.parent = new_parent
    elif action == "delete":
        for child in list(node.children):  # keep sub-levels: they move up one level
            child.parent = node.parent
        db.session.flush()
        db.session.delete(node)
    elif action == "add_member":
        emp = _emp_or_none(f.get("employee_id"))
        if emp is None:
            flash("Employee not found.", "error")
            return False
        if emp not in node.members:
            node.members.append(emp)
    elif action == "remove_member":
        node.members = [m for m in node.members if m.employee_id != f.get("employee_id")]
    else:
        abort(400)
    return None


def _save_settings(app):
    for key, spec in app.all_settings.items():
        raw = request.form.get(key)
        if spec["type"] == "bool":
            value = raw == "1"
        elif spec["type"] == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                flash(f"{spec['label']}: enter a whole number.", "error")
                return False
        elif spec["type"] == "choice":
            value = raw if raw in spec["choices"] else spec["default"]
        else:
            value = (raw or "").strip()
        set_setting(app.code, key, value)


# ---------------------------------------------------------------- sites
@bp.route("/sites", methods=["GET", "POST"])
@superadmin_required
def sites_page():
    from core.models import Site
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            code = request.form.get("code", "").strip()
            name = request.form.get("name", "").strip()
            if not code or not name:
                flash("Site code and name are required.", "error")
            elif Site.query.filter_by(code=code).first():
                flash(f"Site code '{code}' already exists.", "error")
            else:
                site = Site(code=code, name=name, is_active=True)
                db.session.add(site)
                db.session.commit()
                flash(f"Site '{name}' ({code}) added. Now download its sync client.", "success")
                return redirect(url_for("site_sync.client", site_id=site.id))
        elif action == "toggle":
            site_id = request.form.get("site_id", type=int)
            site = db.session.get(Site, site_id)
            if site:
                site.is_active = not site.is_active
                db.session.commit()
                flash(f"Site '{site.name}' status updated.", "success")
        elif action == "edit":
            site_id = request.form.get("site_id", type=int)
            site = db.session.get(Site, site_id)
            if site:
                name = request.form.get("name", "").strip()
                if name:
                    site.name = name
                    db.session.commit()
                    flash(f"Site '{site.code}' updated.", "success")
        return redirect(url_for("admin.sites_page"))

    sites = Site.query.order_by(Site.code).all()
    return render_template("admin/sites.html", sites=sites)

