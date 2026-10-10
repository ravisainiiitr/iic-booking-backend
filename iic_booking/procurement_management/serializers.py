"""Plain-dict serializers (money always as strings)."""

from __future__ import annotations

from decimal import Decimal


def m(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def q(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.001")))


def iso(value) -> str | None:
    return value.isoformat() if value else None


def user_brief(user) -> dict | None:
    if user is None:
        return None
    from iic_booking.users.display import get_user_display_name

    return {"id": user.pk, "name": get_user_display_name(user) or user.email, "email": user.email}


def department_brief(dept) -> dict | None:
    if dept is None:
        return None
    return {"id": dept.pk, "name": dept.name, "code": dept.code or ""}


def equipment_brief(eq) -> dict | None:
    if eq is None:
        return None
    return {"id": eq.pk, "name": eq.name, "code": eq.code}


def lab_brief(lab) -> dict | None:
    if lab is None:
        return None
    return {"id": str(lab.pk), "name": lab.name, "code": lab.code}


def config(cfg) -> dict:
    from .config_service import BOOL_FIELDS, CHOICE_FIELDS, MONEY_FIELDS

    out = {"department": department_brief(cfg.department)}
    for f in BOOL_FIELDS:
        out[f] = getattr(cfg, f)
    for f in MONEY_FIELDS:
        out[f] = m(getattr(cfg, f))
    for f in CHOICE_FIELDS:
        out[f] = getattr(cfg, f)
    out["variance_tolerance_percent"] = m(cfg.variance_tolerance_percent)
    out["current_financial_year"] = cfg.current_financial_year
    out["amc_reminder_days"] = cfg.amc_reminder_days
    out["pilot_users"] = [user_brief(u) for u in cfg.pilot_users.order_by("name")] if cfg.pk else []
    out["updated_at"] = iso(cfg.updated_at) if cfg.pk else None
    out["updated_by"] = user_brief(cfg.updated_by) if cfg.updated_by_id else None
    return out


def role_assignment(row) -> dict:
    return {
        "id": row.pk,
        "department_id": row.department_id,
        "user": user_brief(row.user),
        "role": row.role,
        "permissions": row.permissions or [],
        "equipment_ids": row.equipment_ids or [],
        "active": row.active,
        "updated_at": iso(row.updated_at),
    }


def category(cat) -> dict:
    return {
        "id": cat.pk,
        "code": cat.code,
        "name": cat.name,
        "nature": cat.nature,
        "is_asset": cat.is_asset,
        "tracks_stock": cat.tracks_stock,
        "small_purchase_allowed": cat.small_purchase_allowed,
        "approval_exempt": cat.approval_exempt,
        "hod_required_always": cat.hod_required_always,
        "active": cat.active,
    }


def request_type(rt) -> dict:
    return {
        "id": rt.pk,
        "code": rt.code,
        "name": rt.name,
        "default_nature": rt.default_nature,
        "requires_oic": rt.requires_oic,
        "requires_stores": rt.requires_stores,
        "hod_rule": rt.hod_rule,
        "stores_issue_flow": rt.stores_issue_flow,
        "allow_small_purchase": rt.allow_small_purchase,
        "requires_specification": rt.requires_specification,
        "procurement_steps": rt.procurement_steps or [],
        "active": rt.active,
    }


def gst_rate(row) -> dict:
    return {
        "id": row.pk,
        "name": row.name,
        "rate": m(row.rate),
        "cgst_rate": m(row.cgst_rate),
        "sgst_rate": m(row.sgst_rate),
        "igst_rate": m(row.igst_rate),
        "active": row.active,
    }


def vendor(v) -> dict:
    return {
        "id": v.pk,
        "department_id": v.department_id,
        "code": v.code,
        "name": v.name,
        "gstin": v.gstin,
        "pan": v.pan,
        "address": v.address,
        "state": v.state,
        "contact_person": v.contact_person,
        "phone": v.phone,
        "email": v.email,
        "remarks": v.remarks,
        "active": v.active,
        "is_archived": v.is_archived,
    }


def item(i) -> dict:
    return {
        "id": i.pk,
        "department_id": i.department_id,
        "code": i.code,
        "name": i.name,
        "category": category(i.category),
        "uom": i.uom,
        "specification": i.specification,
        "hsn_sac": i.hsn_sac,
        "default_gst_rate": m(i.default_gst_rate.rate) if i.default_gst_rate_id else None,
        "min_level": q(i.min_level),
        "reorder_level": q(i.reorder_level),
        "part_number": i.part_number,
        "tracks_batch": i.tracks_batch,
        "active": i.active,
        "is_archived": i.is_archived,
    }


def document(d) -> dict:
    return {
        "id": d.pk,
        "doc_type": d.doc_type,
        "original_name": d.original_name,
        "content_type": d.content_type,
        "size_bytes": d.size_bytes,
        "sha256": d.sha256,
        "page_group": str(d.page_group) if d.page_group else None,
        "page_number": d.page_number,
        "description": d.description,
        "uploaded_by": user_brief(d.uploaded_by),
        "uploaded_at": iso(d.created_at),
        "download_url": f"/api/v1/procurement/documents/{d.pk}/download/",
        "is_archived": d.is_archived,
        "links": {
            "purchase_request_id": d.purchase_request_id,
            "procurement_record_id": d.procurement_record_id,
            "invoice_id": d.invoice_id,
            "asset_id": d.asset_id,
            "proposal_id": d.proposal_id,
            "amc_record_id": d.amc_record_id,
            "quotation_id": d.quotation_id,
        },
    }


def approval_action(a) -> dict:
    return {
        "id": a.pk,
        "stage": a.stage,
        "action": a.action,
        "action_label": a.get_action_display(),
        "from_status": a.from_status,
        "to_status": a.to_status,
        "actor": user_brief(a.actor),
        "actor_role": a.actor_role,
        "comments": a.comments,
        "amount": m(a.amount),
        "is_offline": a.is_offline,
        "offline_approver_name": a.offline_approver_name,
        "offline_approver_designation": a.offline_approver_designation,
        "offline_approval_date": iso(a.offline_approval_date),
        "offline_reference": a.offline_reference,
        "offline_document_id": a.offline_document_id,
        "created_at": iso(a.created_at),
    }


def audit_log(row) -> dict:
    return {
        "id": row.pk,
        "department_id": row.department_id,
        "actor": user_brief(row.actor),
        "action": row.action,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "object_number": row.object_number,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "reason": row.reason,
        "ip_address": row.ip_address,
        "created_at": iso(row.created_at),
    }


def request_line(line) -> dict:
    return {
        "id": line.pk,
        "item_id": line.item_id,
        "item_code": line.item.code if line.item_id else "",
        "description": line.description,
        "specification": line.specification,
        "quantity": q(line.quantity),
        "uom": line.uom,
        "estimated_unit_price": m(line.estimated_unit_price),
        "gst_rate": m(line.gst_rate),
        "line_total": m(line.line_total),
        "issued_quantity": q(line.issued_quantity),
        "fulfilment": line.fulfilment,
        "store_note": line.store_note,
        "store_original": line.store_original or {},
        "added_by_stores": line.added_by_stores,
    }


def invoice_line(line) -> dict:
    return {
        "id": line.pk,
        "item_id": line.item_id,
        "description": line.description,
        "quantity": q(line.quantity),
        "uom": line.uom,
        "unit_price": m(line.unit_price),
        "gst_rate": m(line.gst_rate),
        "taxable_amount": m(line.taxable_amount),
        "gst_amount": m(line.gst_amount),
        "line_total": m(line.line_total),
    }


def invoice(inv, *, detail: bool = False) -> dict:
    out = {
        "id": inv.pk,
        "procurement_record_id": inv.procurement_record_id,
        "vendor": {"id": inv.vendor_id, "name": inv.vendor.name} if inv.vendor_id else None,
        "vendor_name": inv.vendor_name_text,
        "invoice_number": inv.invoice_number,
        "invoice_date": iso(inv.invoice_date),
        "supply_type": inv.supply_type,
        "taxable_amount": m(inv.taxable_amount),
        "cgst_amount": m(inv.cgst_amount),
        "sgst_amount": m(inv.sgst_amount),
        "igst_amount": m(inv.igst_amount),
        "other_charges": m(inv.other_charges),
        "total_amount": m(inv.total_amount),
        "approved_amount": m(inv.approved_amount),
        "variance_amount": m(inv.variance_amount),
        "variance_percent": m(inv.variance_percent),
        "variance_status": inv.variance_status,
        "variance_review_note": inv.variance_review_note,
        "variance_reviewed_by": user_brief(inv.variance_reviewed_by) if inv.variance_reviewed_by_id else None,
        "paid_amount": m(inv.paid_amount),
        "payment_status": inv.payment_status,
        "remarks": inv.remarks,
        "recorded_by": user_brief(inv.recorded_by),
        "created_at": iso(inv.created_at),
        "is_archived": inv.is_archived,
        "forwarded_to_accounts_at": iso(inv.forwarded_to_accounts_at),
        "forwarded_by": user_brief(inv.forwarded_by) if inv.forwarded_by_id else None,
        "forward_note": inv.forward_note,
    }
    if detail:
        out["lines"] = [invoice_line(x) for x in inv.lines.all()]
        out["documents"] = [document(d) for d in inv.documents.filter(is_archived=False).select_related("uploaded_by")]
    return out


def quotation(qt) -> dict:
    return {
        "id": qt.pk,
        "vendor": {"id": qt.vendor_id, "name": qt.vendor.name, "gstin": qt.vendor.gstin},
        "quotation_reference": qt.quotation_reference,
        "quotation_date": iso(qt.quotation_date),
        "amount": m(qt.amount),
        "gst_amount": m(qt.gst_amount),
        "total_amount": m(qt.total_amount),
        "delivery_period": qt.delivery_period,
        "warranty": qt.warranty,
        "compliance": qt.compliance,
        "remarks": qt.remarks,
        "is_selected": qt.is_selected,
        "is_archived": qt.is_archived,
    }


RECORD_STEP_FIELDS = (
    "indent_number", "indent_date", "specification", "rfq_reference", "rfq_date", "selection_justification", "po_number",
    "po_date", "expected_delivery_date", "delivery_date", "delivery_challan_number", "inspection_date",
    "inspection_result", "inspection_remarks", "payment_date", "payment_reference", "purchase_date", "purchased_by_name",
    "remarks",
)


def procurement_record(rec, *, detail: bool = False, blockers=None) -> dict:
    out = {
        "id": rec.pk,
        "number": rec.number,
        "title": rec.title,
        "department": department_brief(rec.department),
        "laboratory": lab_brief(rec.laboratory),
        "equipment": equipment_brief(rec.equipment),
        "purchase_request": {"id": rec.purchase_request_id, "number": rec.purchase_request.number} if rec.purchase_request_id else None,
        "proposal_id": rec.proposal_id,
        "category": {"id": rec.category_id, "name": rec.category.name, "is_asset": rec.category.is_asset} if rec.category_id else None,
        "origin": rec.origin,
        "funding_type": rec.funding_type,
        "financial_year": rec.financial_year,
        "is_small_purchase": rec.is_small_purchase,
        "status": rec.status,
        "required_steps": rec.required_steps or [],
        "completed_steps": rec.completed_steps or [],
        "approved_amount": m(rec.approved_amount),
        "estimated_amount": m(rec.estimated_amount),
        "po_amount": m(rec.po_amount),
        "selected_vendor": {"id": rec.selected_vendor_id, "name": rec.selected_vendor.name} if rec.selected_vendor_id else None,
        "payment_status": rec.payment_status,
        "paid_amount": m(rec.paid_amount),
        "purchase_mode": rec.purchase_mode,
        "purchase_mode_reason": rec.purchase_mode_reason,
        "gem_reference": rec.gem_reference,
        "created_by": user_brief(rec.created_by),
        "created_at": iso(rec.created_at),
        "completed_at": iso(rec.completed_at),
    }
    for f in RECORD_STEP_FIELDS:
        v = getattr(rec, f)
        out[f] = iso(v) if hasattr(v, "isoformat") else v
    if detail:
        out["invoices"] = [invoice(i, detail=True) for i in rec.invoices.filter(is_archived=False).select_related("vendor", "recorded_by", "variance_reviewed_by")]
        out["quotations"] = [quotation(x) for x in rec.quotations.filter(is_archived=False).select_related("vendor")]
        out["documents"] = [document(d) for d in rec.documents.filter(is_archived=False).select_related("uploaded_by")]
        out["assets"] = [{"id": a.pk, "number": a.number, "description": a.description, "status": a.status} for a in rec.assets.filter(is_archived=False)]
        out["blockers"] = blockers or []
    return out


def change_log(row) -> dict:
    return {
        "id": row.pk,
        "change_type": row.change_type,
        "field": row.field,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "related_requirement_id": row.related_requirement_id,
        "reason": row.reason,
        "changed_by": user_brief(row.changed_by),
        "changed_at": iso(row.changed_at),
    }


def requirement(r, *, detail: bool = False) -> dict:
    out = {
        "id": r.pk,
        "number": r.number,
        "department": department_brief(r.department),
        "laboratory": lab_brief(r.laboratory),
        "equipment": equipment_brief(r.equipment),
        "financial_year": r.financial_year,
        "funding_type": r.funding_type,
        "category": {"id": r.category_id, "name": r.category.name} if r.category_id else None,
        "description": r.description,
        "quantity": q(r.quantity),
        "uom": r.uom,
        "estimated_unit_cost": m(r.estimated_unit_cost),
        "estimated_total": m(r.estimated_total),
        "approved_amount": m(r.approved_amount),
        "priority": r.priority,
        "status": r.status,
        "raised_by": user_brief(r.raised_by),
        "added_by_office": r.added_by_office,
        "proposal_id": r.proposal_id,
        "merged_into_id": r.merged_into_id,
        "split_from_id": r.split_from_id,
        "procurement_record_id": r.procurement_record_id,
        "submitted_at": iso(r.submitted_at),
        "created_at": iso(r.created_at),
    }
    if detail:
        out["specification"] = r.specification
        out["justification"] = r.justification
        out["original_values"] = r.original_values or {}
        out["changes"] = [change_log(x) for x in r.change_logs.select_related("changed_by")]
    return out


def proposal(p, *, detail: bool = False) -> dict:
    out = {
        "id": p.pk,
        "number": p.number,
        "department": department_brief(p.department),
        "financial_year": p.financial_year,
        "funding_type": p.funding_type,
        "title": p.title,
        "remarks": p.remarks,
        "status": p.status,
        "total_amount": m(p.total_amount),
        "approved_amount": m(p.approved_amount),
        "created_by": user_brief(p.created_by),
        "sent_at": iso(p.sent_at),
        "decided_at": iso(p.decided_at),
        "created_at": iso(p.created_at),
    }
    if detail:
        reqs = p.requirements.select_related("department", "laboratory", "equipment", "category", "raised_by")
        out["requirements"] = [requirement(r) for r in reqs]
        out["history"] = [approval_action(a) for a in p.approval_actions.select_related("actor")]
        out["documents"] = [document(d) for d in p.documents.filter(is_archived=False).select_related("uploaded_by")]
    return out


def purchase_request(r, *, detail: bool = False, scope=None) -> dict:
    out = {
        "id": r.pk,
        "number": r.number,
        "title": r.title,
        "department": department_brief(r.department),
        "laboratory": lab_brief(r.laboratory),
        "equipment": equipment_brief(r.equipment),
        "request_type": {"id": r.request_type_id, "code": r.request_type.code, "name": r.request_type.name},
        "category": {"id": r.category_id, "name": r.category.name, "nature": r.category.nature} if r.category_id else None,
        "nature": r.nature,
        "origin": r.origin,
        "funding_type": r.funding_type,
        "financial_year": r.financial_year,
        "priority": r.priority,
        "status": r.status,
        "status_label": r.get_status_display(),
        "current_stage": r.current_stage,
        "approval_route": r.approval_route or [],
        "route_index": r.route_index,
        "requested_by": user_brief(r.requested_by),
        "raised_as_role": r.raised_as_role,
        "estimated_total": m(r.estimated_total),
        "approved_amount": m(r.approved_amount),
        "is_small_purchase": r.is_small_purchase,
        "hod_required": r.hod_required,
        "last_reason": r.last_reason,
        "required_by": iso(r.required_by),
        "submitted_at": iso(r.submitted_at),
        "approved_at": iso(r.approved_at),
        "completed_at": iso(r.completed_at),
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
        "stage_entered_at": iso(r.stage_entered_at),
        "stage_age_days": stage_age_days(r),
        "maintenance_record_id": r.maintenance_record_id,
        "disruption_event_id": r.disruption_event_id,
    }
    if detail:
        out["justification"] = r.justification
        out["specification"] = r.specification
        out["resubmission_count"] = r.resubmission_count
        out["lines"] = [request_line(line) for line in r.lines.select_related("item")]
        out["documents"] = [document(d) for d in r.documents.filter(is_archived=False).select_related("uploaded_by")]
        out["history"] = [approval_action(a) for a in r.approval_actions.select_related("actor")]
        out["procurement_record_ids"] = list(r.procurement_records.values_list("id", flat=True))
        if scope is not None:
            from .workflow import available_actions

            out["available_actions"] = available_actions(r, scope)
    return out


def stage_age_days(r) -> int | None:
    from django.utils import timezone

    from . import constants as c

    if r.status not in c.PENDING_STATUSES:
        return None
    since = r.stage_entered_at or r.submitted_at
    return (timezone.now() - since).days if since else None


def _vendor(v) -> dict | None:
    return {"id": v.pk, "name": v.name} if v is not None else None


def asset_transfer(t) -> dict:
    return {
        "id": t.pk,
        "number": t.number,
        "asset": {"id": t.asset_id, "number": t.asset.number, "description": t.asset.description},
        "transfer_type": t.transfer_type,
        "status": t.status,
        "from_laboratory": lab_brief(t.from_laboratory),
        "to_laboratory": lab_brief(t.to_laboratory),
        "from_equipment": equipment_brief(t.from_equipment),
        "to_equipment": equipment_brief(t.to_equipment),
        "from_location": t.from_location,
        "to_location": t.to_location,
        "from_custodian": user_brief(t.from_custodian),
        "to_custodian": user_brief(t.to_custodian),
        "from_status": t.from_status,
        "reason": t.reason,
        "expected_return_date": iso(t.expected_return_date),
        "requested_by": user_brief(t.requested_by),
        "decided_by": user_brief(t.decided_by),
        "decided_at": iso(t.decided_at),
        "decision_note": t.decision_note,
        "completed_at": iso(t.completed_at),
        "returned_at": iso(t.returned_at),
        "created_at": iso(t.created_at),
    }


def asset(a, *, detail: bool = False) -> dict:
    out = {
        "id": a.pk,
        "number": a.number,
        "department": department_brief(a.department),
        "laboratory": lab_brief(a.laboratory),
        "equipment": equipment_brief(a.equipment),
        "category": {"id": a.category_id, "name": a.category.name, "nature": a.category.nature},
        "item_id": a.item_id,
        "description": a.description,
        "make": a.make,
        "model_number": a.model_number,
        "serial_number": a.serial_number,
        "asset_tag": a.asset_tag,
        "procurement_record": {"id": a.procurement_record_id, "number": a.procurement_record.number} if a.procurement_record_id else None,
        "invoice_id": a.invoice_id,
        "vendor": _vendor(a.vendor),
        "purchase_date": iso(a.purchase_date),
        "cost": m(a.cost),
        "is_capitalized": a.is_capitalized,
        "funding_type": a.funding_type,
        "financial_year": a.financial_year,
        "warranty_until": iso(a.warranty_until),
        "location": a.location,
        "custodian": user_brief(a.custodian),
        "status": a.status,
        "status_label": a.get_status_display(),
        "remarks": a.remarks,
        "created_by": user_brief(a.created_by),
        "created_at": iso(a.created_at),
        "register": register_brief(a.register) if a.register_id else None,
        "register_page": a.register_page,
        "register_serial": a.register_serial,
        "register_entry_date": iso(a.register_entry_date),
        "register_ref": a.register_ref,
        "legacy_ref": a.legacy_ref,
        "parent": {"id": a.parent_id, "number": a.parent.number, "description": a.parent.description,
                   "asset_tag": a.parent.asset_tag} if a.parent_id else None,
        "quantity": a.quantity,
        "supplier_name": a.supplier_name,
        "po_number": a.po_number,
        "po_date": iso(a.po_date),
        "invoice_number": a.invoice_number,
        "invoice_date": iso(a.invoice_date),
        "funding_source": a.funding_source,
        "project_code": a.project_code,
        "installation_date": iso(a.installation_date),
        "amc_until": iso(a.amc_until),
        "condition": a.condition,
        "useful_life_years": a.useful_life_years,
        "depreciation_rate": m(a.depreciation_rate),
        "last_verified_on": iso(a.last_verified_on),
        "last_verification_result": a.last_verification_result,
    }
    if detail:
        out["accessories"] = [
            {"id": x.pk, "number": x.number, "description": x.description, "asset_tag": x.asset_tag,
             "register_ref": x.register_ref, "status": x.status, "cost": m(x.cost)}
            for x in a.accessories.filter(is_archived=False).select_related("register")
        ]
        out["verifications"] = [asset_verification(v) for v in a.verifications.select_related("verified_by", "campaign")[:50]]
        out["disposals"] = [asset_disposal(d) for d in a.disposals.select_related("recorded_by")]
        out["maintenance_records"] = [
            {"id": x.pk, "number": x.number, "kind": x.kind, "downtime_start": iso(x.downtime_start),
             "downtime_end": iso(x.downtime_end), "total_cost": m(x.total_cost)}
            for x in a.maintenance_records.filter(is_archived=False)[:50]
        ]
        out["status_history"] = [
            {"from_status": h.from_status, "to_status": h.to_status, "reason": h.reason,
             "changed_by": user_brief(h.changed_by), "changed_at": iso(h.changed_at)}
            for h in a.status_history.select_related("changed_by")
        ]
        out["transfers"] = [
            asset_transfer(t)
            for t in a.transfers.select_related(
                "asset", "from_laboratory", "to_laboratory", "from_equipment", "to_equipment", "from_custodian",
                "to_custodian", "requested_by", "decided_by",
            )
        ]
        out["documents"] = [document(d) for d in a.documents.filter(is_archived=False).select_related("uploaded_by")]
        out["amc_records"] = [{"id": x.pk, "number": x.number, "status": x.status, "end_date": iso(x.end_date)} for x in a.amc_records.all()]
    return out


def stock_balance(b) -> dict:
    return {
        "id": b.pk,
        "department_id": b.department_id,
        "laboratory": lab_brief(b.laboratory),
        "item": {"id": b.item_id, "code": b.item.code, "name": b.item.name, "uom": b.item.uom},
        "quantity": q(b.quantity),
        "min_level": q(b.min_level),
        "reorder_level": q(b.reorder_level),
        "below_min": b.min_level > 0 and b.quantity < b.min_level,
        "reorder_due": b.reorder_level > 0 and b.quantity <= b.reorder_level,
        "updated_at": iso(b.updated_at),
    }


def stock_transaction(t) -> dict:
    return {
        "id": t.pk,
        "number": t.number,
        "department_id": t.department_id,
        "laboratory": lab_brief(t.laboratory),
        "item": {"id": t.item_id, "code": t.item.code, "name": t.item.name, "uom": t.item.uom},
        "tx_type": t.tx_type,
        "quantity": q(t.quantity),
        "signed_quantity": q(t.signed_quantity),
        "balance_after": q(t.balance_after),
        "unit_cost": m(t.unit_cost),
        "transaction_date": iso(t.transaction_date),
        "reference_type": t.reference_type,
        "reference_number": t.reference_number,
        "purchase_request_id": t.purchase_request_id,
        "procurement_record_id": t.procurement_record_id,
        "invoice_id": t.invoice_id,
        "issued_to": user_brief(t.issued_to),
        "remarks": t.remarks,
        "performed_by": user_brief(t.performed_by),
        "created_at": iso(t.created_at),
        "batch_number": t.batch_number,
        "expiry_date": iso(t.expiry_date),
        "reason_code": t.reason_code,
        "equipment_id": t.equipment_id,
        "maintenance_record_id": t.maintenance_record_id,
    }


def amc_record(r, *, detail: bool = False, today=None) -> dict:
    from django.utils import timezone

    out = {
        "id": r.pk,
        "number": r.number,
        "department": department_brief(r.department),
        "equipment": equipment_brief(r.equipment),
        "asset": {"id": r.asset_id, "number": r.asset.number} if r.asset_id else None,
        "vendor": _vendor(r.vendor),
        "contract_type": r.contract_type,
        "contract_reference": r.contract_reference,
        "start_date": iso(r.start_date),
        "end_date": iso(r.end_date),
        "days_left": (r.end_date - (today or timezone.localdate())).days,
        "contract_value": m(r.contract_value),
        "gst_amount": m(r.gst_amount),
        "total_value": m(r.total_value),
        "coverage": r.coverage,
        "status": r.status,
        "renewed_from_id": r.renewed_from_id,
        "procurement_record_id": r.procurement_record_id,
        "reminder_sent_at": iso(r.reminder_sent_at),
        "created_by": user_brief(r.created_by),
        "created_at": iso(r.created_at),
    }
    if detail:
        out["renewals"] = [{"id": x.pk, "number": x.number, "status": x.status} for x in r.renewals.all()]
        out["documents"] = [document(d) for d in r.documents.filter(is_archived=False).select_related("uploaded_by")]
    return out


def register_brief(reg) -> dict | None:
    if reg is None:
        return None
    return {"id": reg.pk, "code": reg.code, "name": reg.name, "register_type": reg.register_type, "volume": reg.volume}


def asset_register(reg, *, entries: int | None = None) -> dict:
    return {
        **register_brief(reg),
        "department": department_brief(reg.department),
        "register_type_label": reg.get_register_type_display(),
        "laboratory": lab_brief(reg.laboratory),
        "custodian": user_brief(reg.custodian),
        "opened_on": iso(reg.opened_on),
        "closed_on": iso(reg.closed_on),
        "total_pages": reg.total_pages,
        "remarks": reg.remarks,
        "active": reg.active,
        "entry_count": entries,
        "created_at": iso(reg.created_at),
    }


def asset_verification(v) -> dict:
    return {
        "id": v.pk,
        "asset_id": v.asset_id,
        "campaign": {"id": v.campaign_id, "number": v.campaign.number, "title": v.campaign.title} if v.campaign_id else None,
        "verified_on": iso(v.verified_on),
        "verified_by": user_brief(v.verified_by),
        "result": v.result,
        "result_label": v.get_result_display(),
        "condition": v.condition,
        "quantity_found": v.quantity_found,
        "location_seen": v.location_seen,
        "remarks": v.remarks,
        "method": v.method,
        "created_at": iso(v.created_at),
    }


def verification_campaign(cmp, *, stats: dict | None = None) -> dict:
    return {
        "id": cmp.pk,
        "number": cmp.number,
        "department": department_brief(cmp.department),
        "title": cmp.title,
        "financial_year": cmp.financial_year,
        "register": register_brief(cmp.register) if cmp.register_id else None,
        "laboratory": lab_brief(cmp.laboratory),
        "committee": cmp.committee,
        "status": cmp.status,
        "started_on": iso(cmp.started_on),
        "closed_on": iso(cmp.closed_on),
        "remarks": cmp.remarks,
        "created_by": user_brief(cmp.created_by),
        "closed_by": user_brief(cmp.closed_by) if cmp.closed_by_id else None,
        "stats": stats or {},
    }


def asset_disposal(d) -> dict:
    return {
        "id": d.pk,
        "number": d.number,
        "asset_id": d.asset_id,
        "action": d.action,
        "action_label": d.get_action_display(),
        "mode": d.mode,
        "board_reference": d.board_reference,
        "sanction_reference": d.sanction_reference,
        "sanction_date": iso(d.sanction_date),
        "book_value": m(d.book_value),
        "realised_value": m(d.realised_value),
        "from_status": d.from_status,
        "to_status": d.to_status,
        "remarks": d.remarks,
        "recorded_by": user_brief(d.recorded_by),
        "recorded_at": iso(d.recorded_at),
    }


def item_link(link) -> dict:
    return {
        "id": link.pk,
        "department_id": link.department_id,
        "item": {"id": link.item_id, "code": link.item.code, "name": link.item.name, "uom": link.item.uom,
                 "part_number": link.item.part_number},
        "equipment": equipment_brief(link.equipment),
        "usage": link.usage,
        "typical_quantity": q(link.typical_quantity),
        "notes": link.notes,
        "active": link.active,
    }


def maintenance_record(rec, *, detail: bool = False) -> dict:
    out = {
        "id": rec.pk,
        "number": rec.number,
        "department": department_brief(rec.department),
        "equipment": equipment_brief(rec.equipment),
        "asset": {"id": rec.asset_id, "number": rec.asset.number, "description": rec.asset.description} if rec.asset_id else None,
        "disruption_event_id": rec.disruption_event_id,
        "amc_record_id": rec.amc_record_id,
        "kind": rec.kind,
        "kind_label": rec.get_kind_display(),
        "downtime_start": iso(rec.downtime_start),
        "downtime_end": iso(rec.downtime_end),
        "downtime_hours": rec.downtime_hours,
        "cause": rec.cause,
        "action_taken": rec.action_taken,
        "vendor": _vendor(rec.vendor),
        "service_provider": rec.service_provider,
        "service_report_reference": rec.service_report_reference,
        "service_cost": m(rec.service_cost),
        "other_cost": m(rec.other_cost),
        "parts_cost": m(rec.parts_cost),
        "total_cost": m(rec.total_cost),
        "under_warranty_or_amc": rec.under_warranty_or_amc,
        "remarks": rec.remarks,
        "recorded_by": user_brief(rec.recorded_by),
        "created_at": iso(rec.created_at),
    }
    if detail:
        out["parts_used"] = [
            stock_transaction(tx)
            for tx in rec.parts_used.select_related("item", "laboratory", "issued_to", "performed_by")
        ]
        out["requests"] = [
            {"id": r.pk, "number": r.number, "title": r.title, "status": r.status, "estimated_total": m(r.estimated_total)}
            for r in rec.requests.all()
        ]
        out["documents"] = [document(d) for d in rec.documents.filter(is_archived=False).select_related("uploaded_by")]
    return out
