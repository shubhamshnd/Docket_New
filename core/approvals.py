"""Generic approval engine driven by each app's approval org chart (core.models.ApprovalNode).

Routing (same idea as Docket's department branches):
- Applicant sits in the chart -> the request starts at the level above their (deepest) box.
- Otherwise it starts at the deepest box tagged with the applicant's department,
  else at a box marked "everyone else". No match -> submission is blocked with a clear message.
- From there it climbs parent by parent to the top. Levels with an unmet condition are passed over.
Rules:
- The applicant is never their own approver (that level is skipped).
- Someone who already approved earlier in the same round is auto-approved at later levels.
- Any person in a level can act; app admins can act on anyone's behalf.
- Reject / send back need remarks. Send back lets the applicant edit and resubmit (new round).
"""
from datetime import datetime

from flask import current_app, has_request_context, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from core.access import can
from core.db import db
from core.mail import send_mail
from core.models import ApprovalNode, ApprovalTask, Employee, Notification
from core.notify import notify
from core.registry import APPS

OPEN = ("waiting", "pending")
TOKEN_SALT = "docket-email-approval"


class ApprovalError(Exception):
    pass


def make_action_token(task_id, emp_id, action):
    """Generate a secure, signed URL-safe token for one-click email approval/rejection."""
    secret = current_app.config.get("SECRET_KEY", "docket-secret-approval")
    s = URLSafeTimedSerializer(secret, salt=TOKEN_SALT)
    return s.dumps({"task_id": task_id, "emp_id": emp_id, "action": action})


def verify_action_token(token, max_age=86400 * 14):
    """Verify signed token and return (task, actor, action) or raise ApprovalError."""
    secret = current_app.config.get("SECRET_KEY", "docket-secret-approval")
    s = URLSafeTimedSerializer(secret, salt=TOKEN_SALT)
    try:
        data = s.loads(token, max_age=max_age)
    except SignatureExpired:
        raise ApprovalError("This approval link has expired.")
    except BadSignature:
        raise ApprovalError("Invalid or corrupted approval link.")
    task = db.session.get(ApprovalTask, data.get("task_id"))
    if not task:
        raise ApprovalError("Approval task no longer exists.")
    actor = db.session.get(Employee, data.get("emp_id"))
    if not actor:
        raise ApprovalError("Approver employee account not found.")
    return task, actor, data.get("action")


def send_approval_request_email(task, record, earlier_info=""):
    """Sends personalized email with one-click approve/reject buttons to each candidate approver."""
    candidates = task.candidate_people
    if not candidates:
        return

    # In-app notifications
    for person in candidates:
        db.session.add(Notification(
            employee_id=person.employee_id,
            app_code=task.app_code,
            ref_id=task.ref_id,
            kind="action",
            title=f"Approval needed: {record.approval_title}",
            body=f"Request {record.approval_title} is pending your approval at level: {task.label}.",
            url=_url(record),
        ))

    base_url = ""
    if has_request_context():
        base_url = request.host_url.rstrip("/")
    else:
        base_url = current_app.config.get("BASE_URL") or "http://localhost:5050"
        base_url = base_url.rstrip("/")

    req_no = getattr(record, "req_no", record.approval_title)
    applicant = getattr(record, "applicant", None)
    app_name = applicant.name if applicant else "Employee"
    app_dept = getattr(record, "department", None) or (applicant.department if applicant else "") or "-"
    app_desig = (applicant.designation if applicant else "") or "-"
    view_url = f"{base_url}{_url(record)}"

    # Requisition details for guest_house
    visit_type = getattr(record, "visit_type", "")
    purpose = getattr(record, "purpose", "")
    guests_list = getattr(record, "guests", [])
    guest_count = len(guests_list) if guests_list else 0
    first_arr = getattr(record, "first_arrival", None)
    last_dep = getattr(record, "last_departure", None)
    arr_str = first_arr.strftime("%d %b %Y %H:%M") if first_arr else "-"
    dep_str = last_dep.strftime("%d %b %Y %H:%M") if last_dep else "-"

    req_items = []
    if getattr(record, "need_guest_house", False): req_items.append("Guest House")
    if getattr(record, "need_hotel", False): req_items.append("Hotel")
    if getattr(record, "need_vehicle", False): req_items.append("Vehicle")
    if getattr(record, "need_food", False): req_items.append("Food")
    if getattr(record, "need_ticket", False): req_items.append("Tickets")
    req_str = ", ".join(req_items) if req_items else "Standard"

    for person in candidates:
        if not person.email:
            continue

        token_approve = make_action_token(task.id, person.employee_id, "approve")
        token_reject = make_action_token(task.id, person.employee_id, "reject")
        approve_url = f"{base_url}/approvals/email-action/{token_approve}"
        reject_url = f"{base_url}/approvals/email-action/{token_reject}"

        plain_text = (
            f"Dear {person.name},\n\n"
            f"A requisition is pending your approval at level: {task.label}{earlier_info}.\n\n"
            f"Request No: {req_no}\n"
            f"Applicant: {app_name} ({app_desig} · {app_dept})\n"
            f"Dates: {arr_str} to {dep_str} ({guest_count} guest{'s' if guest_count != 1 else ''})\n"
            f"Requirements: {req_str}\n\n"
            f"To APPROVE directly from this email:\n{approve_url}\n\n"
            f"To REJECT directly from this email:\n{reject_url}\n\n"
            f"Or view details in Docket:\n{view_url}\n\n"
            f"Best regards,\nDocket Administration"
        )

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 620px; margin: 0 auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
          <div style="background: #0284c7; padding: 20px 24px; color: #ffffff;">
            <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; opacity: 0.9;">Docket Approval Workflow</div>
            <h1 style="margin: 6px 0 0; font-size: 20px; font-weight: 700; color: #ffffff;">Approval Needed: {req_no}</h1>
          </div>
          <div style="padding: 24px;">
            <p style="margin-top: 0; font-size: 15px; color: #334155;">Dear <b>{person.name}</b>,</p>
            <p style="font-size: 14px; color: #475569; line-height: 1.5;">The following requisition has been submitted and requires your approval at <b>{task.label}</b>{earlier_info}:</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 18px 0; background: #f8fafc; border-radius: 6px; border: 1px solid #e2e8f0; font-size: 13.5px;">
              <tr>
                <td style="padding: 9px 14px; font-weight: 600; color: #64748b; width: 32%; border-bottom: 1px solid #e2e8f0;">Request No:</td>
                <td style="padding: 9px 14px; font-weight: 700; color: #0284c7; border-bottom: 1px solid #e2e8f0;">{req_no}</td>
              </tr>
              <tr>
                <td style="padding: 9px 14px; font-weight: 600; color: #64748b; border-bottom: 1px solid #e2e8f0;">Applicant:</td>
                <td style="padding: 9px 14px; color: #0f172a; border-bottom: 1px solid #e2e8f0;"><b>{app_name}</b> ({app_desig} · {app_dept})</td>
              </tr>
              {"<tr><td style='padding: 9px 14px; font-weight: 600; color: #64748b; border-bottom: 1px solid #e2e8f0;'>Visit Purpose:</td><td style='padding: 9px 14px; color: #0f172a; border-bottom: 1px solid #e2e8f0;'>" + (visit_type + (" - " + purpose if purpose else "")) + "</td></tr>" if visit_type else ""}
              {"<tr><td style='padding: 9px 14px; font-weight: 600; color: #64748b; border-bottom: 1px solid #e2e8f0;'>Guests & Dates:</td><td style='padding: 9px 14px; color: #0f172a; border-bottom: 1px solid #e2e8f0;'>" + f"{guest_count} guest(s) · {arr_str} to {dep_str}" + "</td></tr>" if guest_count else ""}
              {"<tr><td style='padding: 9px 14px; font-weight: 600; color: #64748b; border-bottom: 1px solid #e2e8f0;'>Requirements:</td><td style='padding: 9px 14px; color: #0f172a; border-bottom: 1px solid #e2e8f0;'>" + req_str + "</td></tr>" if req_str else ""}
            </table>

            <div style="text-align: center; margin: 28px 0 20px;">
              <a href="{approve_url}" style="display: inline-block; background: #16a34a; color: #ffffff; text-decoration: none; padding: 12px 28px; font-size: 15px; font-weight: 700; border-radius: 6px; margin: 6px 8px; box-shadow: 0 2px 4px rgba(22, 163, 74, 0.25);">
                ✓ Approve Request
              </a>
              <a href="{reject_url}" style="display: inline-block; background: #dc2626; color: #ffffff; text-decoration: none; padding: 12px 28px; font-size: 15px; font-weight: 700; border-radius: 6px; margin: 6px 8px; box-shadow: 0 2px 4px rgba(220, 38, 38, 0.25);">
                ✗ Reject Request
              </a>
            </div>
            
            <div style="text-align: center; margin-top: 14px;">
              <a href="{view_url}" style="font-size: 13px; color: #0284c7; text-decoration: none; font-weight: 500;">
                Review full details in Docket →
              </a>
            </div>
          </div>
          <div style="background: #f8fafc; padding: 14px 24px; font-size: 11.5px; color: #94a3b8; text-align: center; border-top: 1px solid #e2e8f0;">
            This email was sent by Docket. Clicking Approve or Reject will take you to a secure one-click confirmation screen.
          </div>
        </div>
        """

        send_mail([person], f"[Docket] Approval needed: {record.approval_title}", plain_text, html_body=html_body)


def route(app_code, applicant):
    """Boxes from the entry point up to the top, or raise ApprovalError. [] when the app has no chart."""
    nodes_q = ApprovalNode.query.filter_by(app_code=app_code)
    applicant_site_id = getattr(applicant, "site_id", None)
    if applicant_site_id:
        site_nodes = nodes_q.filter_by(site_id=applicant_site_id).all()
        nodes = site_nodes if site_nodes else nodes_q.filter(ApprovalNode.site_id.is_(None)).all()
    else:
        nodes = nodes_q.all()
    if not nodes:
        return []
    deepest = lambda ns: max(ns, key=lambda n: (n.depth, -n.id))  # noqa: E731
    mine = [n for n in nodes if applicant in n.members]
    if mine:
        start = deepest(mine).parent
    else:
        entries = [n for n in nodes if applicant.department and applicant.department in n.entry_list] \
            or [n for n in nodes if n.entry_all]
        if not entries:
            raise ApprovalError(f'No approval route for department "{applicant.department or "-"}". '
                                "Ask the application admin to add it to the approval org chart.")
        start = deepest(entries)
    path, seen = [], set()
    while start is not None and start.id not in seen:
        seen.add(start.id)
        path.append(start)
        start = start.parent
    return path


def plan(app_code, applicant, context):
    """Dry run: [{label, approvers, note, error}] - also shown on the request form."""
    try:
        path = route(app_code, applicant)
    except ApprovalError as exc:
        return [{"label": "Route", "approvers": [], "note": "", "error": str(exc)}]
    rows = []
    for node in path:
        if node.condition and not context.get(node.condition):
            continue
        people = [e for e in node.members if e.is_active]
        approvers = [e for e in people if e.employee_id != applicant.employee_id]
        row = {"label": node.label, "approvers": approvers, "note": "", "error": ""}
        if not people:
            row["error"] = f'Nobody active in "{node.label}". Ask the application admin to update the approval org chart.'
        elif not approvers:
            row["note"] = "Skipped - you are the approver"
        rows.append(row)
    return rows


def start(app_code, ref_id, applicant, context):
    """Create tasks for a (re)submission. Returns 'pending' or 'approved' (nothing to approve)."""
    rows = plan(app_code, applicant, context)
    for row in rows:
        if row["error"]:
            raise ApprovalError(row["error"])
    cancel(app_code, ref_id)
    last = db.session.query(func.max(ApprovalTask.round)).filter_by(app_code=app_code, ref_id=ref_id).scalar()
    rnd = (last or 0) + 1
    for i, row in enumerate(rows):
        ids = [e.employee_id for e in row["approvers"]]
        db.session.add(ApprovalTask(
            app_code=app_code, ref_id=ref_id, round=rnd, position=i, label=row["label"],
            candidates="," + ",".join(ids) + ",", status="waiting" if ids else "skipped", note=row["note"],
        ))
    db.session.flush()
    return _advance(app_code, ref_id, rnd)


def _advance(app_code, ref_id, rnd):
    tasks = ApprovalTask.query.filter_by(app_code=app_code, ref_id=ref_id, round=rnd).order_by(ApprovalTask.position).all()
    approved_by = {t.acted_by_id for t in tasks if t.status == "approved" and t.acted_by_id}
    now = datetime.now()
    for task in tasks:
        if task.status != "waiting":
            continue
        already = approved_by.intersection(task.candidate_ids)
        if already:
            task.status, task.acted_by_id, task.acted_at = "approved", sorted(already)[0], now
            task.note = "Auto-approved - already approved at an earlier level"
            continue
        task.status, task.activated_at = "pending", now
        record = _record(task)
        earlier_approved = [t for t in tasks if t.status == "approved" and t.acted_by and t.position < task.position]
        earlier_info = ""
        if earlier_approved:
            e_parts = [f"{t.label}" for t in earlier_approved]
            earlier_info = f"\nApproved earlier at: {', '.join(e_parts)}"

        send_approval_request_email(task, record, earlier_info=earlier_info)
        return "pending"
    return "approved"


def act(task, actor, action, remarks=""):
    """action: approve / reject / return. Returns the record outcome: pending / approved / rejected / returned."""
    if not can_act(task, actor):
        raise ApprovalError("You cannot act on this approval.")
    if action not in ("approve", "reject", "return"):
        raise ApprovalError("Unknown action.")
    remarks = (remarks or "").strip()
    if action != "approve" and not remarks:
        raise ApprovalError("Remarks are required to reject or send back.")

    task.status = {"approve": "approved", "reject": "rejected", "return": "returned"}[action]
    task.acted_by_id, task.acted_at, task.remarks = actor.employee_id, datetime.now(), remarks
    if actor.employee_id not in task.candidate_ids:
        task.note = "Acted on behalf by application admin"
    # Their "approval needed" notification is done with.
    from core.notify import mark_read
    for person in task.candidate_people:
        mark_read(person, task.app_code, task.ref_id)

    if action == "approve":
        outcome = _advance(task.app_code, task.ref_id, task.round)
        if outcome == "pending":
            record = _record(task)
            next_task = ApprovalTask.query.filter_by(app_code=task.app_code, ref_id=task.ref_id, round=task.round, status="pending").first()
            next_label = next_task.label if next_task else "next level"
            next_people = next_task.candidate_people if next_task else []
            next_names = " / ".join(p.name for p in next_people) if next_people else next_label

            req_no_val = getattr(record, 'req_no', record.approval_title)
            applicant_msg = (
                f"Dear {record.applicant.name},\n\n"
                f"Your requisition {req_no_val} has been approved at level: {task.label}.\n\n"
                f"Current Status: Pending approval at {next_label} ({next_names}).\n\n"
                f"Best regards,\nAdmin"
            )
            applicant_html = (
                f'<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #000000; line-height: 1.5; margin: 0; padding: 12px;">'
                f'<p style="margin: 0 0 12px 0;">Dear {record.applicant.name},</p>'
                f'<p style="margin: 0 0 16px 0;">Your requisition {req_no_val} has been approved at level: <b>{task.label}</b>.</p>'
                f'<table style="border-collapse: collapse; border: 1px solid #000000; font-family: Arial, Helvetica, sans-serif; font-size: 13.5px; width: 100%; max-width: 580px; margin: 16px 0;">'
                f'<tbody>'
                f'<tr>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; width: 130px; color: #000000; vertical-align: top; white-space: nowrap;">Request No:</td>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top;">{req_no_val}</td>'
                f'</tr>'
                f'<tr>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top; white-space: nowrap;">Approved Level:</td>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top;">{task.label}</td>'
                f'</tr>'
                f'<tr>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top; white-space: nowrap;">Current Status:</td>'
                f'<td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top;">Pending approval at {next_label} ({next_names})</td>'
                f'</tr>'
                + (f'<tr><td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top; white-space: nowrap;">Remarks:</td><td style="border: 1px solid #000000; padding: 6px 12px; color: #000000; vertical-align: top;">{remarks}</td></tr>' if remarks else "")
                + f'</tbody>'
                f'</table>'
                f'<p style="margin: 16px 0 0 0;">Best regards,<br>Admin</p>'
                f'</div>'
            )
            cc_people = []
            try:
                if task.app_code == "guest_house":
                    from modules.guest_house.views import get_applicant_hierarchy
                    cc_people = get_applicant_hierarchy(record)
            except Exception:
                pass
            if not cc_people:
                all_tasks = ApprovalTask.query.filter_by(app_code=task.app_code, ref_id=task.ref_id, round=task.round).all()
                seen = {record.applicant.employee_id}
                if getattr(record.applicant, "manager", None) and record.applicant.manager.employee_id not in seen:
                    seen.add(record.applicant.manager.employee_id)
                    cc_people.append(record.applicant.manager)
                for t in all_tasks:
                    if t.acted_by and t.acted_by.employee_id not in seen:
                        seen.add(t.acted_by.employee_id)
                        cc_people.append(t.acted_by)

            notify([record.applicant], task.app_code, task.ref_id,
                   f"{record.approval_title}: approved at {task.label}", _url(record),
                   applicant_msg, email=True, cc_people=cc_people, include_link_in_email=False, html_body=applicant_html)
    else:
        ApprovalTask.query.filter_by(app_code=task.app_code, ref_id=task.ref_id, round=task.round, status="waiting") \
            .update({"status": "cancelled"})
        outcome = task.status
    if outcome != "pending" and APPS[task.app_code].on_approval:
        APPS[task.app_code].on_approval(task.ref_id, outcome, actor, remarks)
    return outcome


def cancel(app_code, ref_id):
    ApprovalTask.query.filter(ApprovalTask.app_code == app_code, ApprovalTask.ref_id == ref_id,
                              ApprovalTask.status.in_(OPEN)).update({"status": "cancelled"}, synchronize_session=False)


def can_act(task, emp):
    return task.status == "pending" and (emp.employee_id in task.candidate_ids or can(emp, task.app_code, "admin"))


def pending_for(emp, app_code=None):
    q = ApprovalTask.query.filter(ApprovalTask.status == "pending",
                                  ApprovalTask.candidates.contains(f",{emp.employee_id},", autoescape=True))
    if app_code:
        q = q.filter(ApprovalTask.app_code == app_code)
    return q.order_by(ApprovalTask.activated_at).all()


def current_task(app_code, ref_id):
    return ApprovalTask.query.filter_by(app_code=app_code, ref_id=ref_id, status="pending").first()


def timeline(app_code, ref_id):
    return ApprovalTask.query.filter_by(app_code=app_code, ref_id=ref_id) \
        .order_by(ApprovalTask.round, ApprovalTask.position).all()


def _record(task):
    return APPS[task.app_code].approval_record(task.ref_id)


def _url(record):
    try:
        return url_for(record.approval_endpoint, **record.approval_args)
    except RuntimeError:  # no request context (CLI)
        return ""
