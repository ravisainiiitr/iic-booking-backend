"""Learning API (``/api/v1/learning/``): modules, quizzes, progress, the training prerequisite switch, logbook."""

from __future__ import annotations

from functools import wraps

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import service as svc
from .models import LearningModule, LogbookEntry, LogbookKind, ModuleProgress, QuizQuestion


def learning_api(methods, parsers=None):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except svc.LearningError as exc:
                return Response({"detail": exc.message, "code": exc.code, **exc.extra}, status=exc.status)

        view = permission_classes([IsAuthenticated])(inner)
        if parsers:
            view = parser_classes(parsers)(view)
        return api_view(methods)(view)

    return deco


def _iso(v):
    return v.isoformat() if v else None


def _equipment(pk):
    from iic_booking.equipment.models import Equipment

    return get_object_or_404(Equipment, pk=pk)


def question_out(q: QuizQuestion, staff: bool) -> dict:
    out = {"id": q.pk, "text": q.text, "options": q.options, "order": q.order,
           "multiple": len(q.correct) > 1}
    if staff:
        out.update({"correct": q.correct, "explanation": q.explanation, "is_active": q.is_active})
    return out


def progress_out(m: LearningModule, p: ModuleProgress | None) -> dict:
    return {
        "viewed_at": _iso(p.viewed_at) if p else None,
        "acknowledged_at": _iso(p.acknowledged_at) if p else None,
        "attempts": p.attempts if p else 0,
        "best_score_pct": p.best_score_pct if p else None,
        "passed_at": _iso(p.passed_at) if p else None,
        "complete": svc.is_complete(m, p),
    }


def module_out(m: LearningModule, user, *, detail: bool = False, progress: ModuleProgress | None = None) -> dict:
    staff = svc.is_staff_for(user, m.equipment_id)
    out = {
        "id": m.pk,
        "equipment_id": m.equipment_id,
        "title": m.title,
        "summary": m.summary,
        "video_url": m.video_url,
        "document_url": m.document.url if m.document else None,
        "order": m.order,
        "is_required": m.is_required,
        "requires_acknowledgement": m.requires_acknowledgement,
        "acknowledgement_text": m.acknowledgement_text,
        "pass_mark_pct": m.pass_mark_pct,
        "max_attempts_per_day": m.max_attempts_per_day,
        "is_published": m.is_published,
        "question_count": m.questions.filter(is_active=True).count(),
        "progress": progress_out(m, progress),
        "can_edit": staff,
    }
    if detail:
        qs = m.questions.all() if staff else m.questions.filter(is_active=True)
        out["body"] = m.body
        out["questions"] = [question_out(q, staff) for q in qs]
    return out


def entry_out(e: LogbookEntry) -> dict:
    from iic_booking.users.display import get_user_display_name

    return {
        "id": e.pk,
        "booking_id": e.booking_id,
        "kind": e.kind,
        "kind_label": e.get_kind_display(),
        "text": e.text,
        "author": {"id": e.author_id, "name": get_user_display_name(e.author)} if e.author_id else None,
        "created_at": _iso(e.created_at),
    }


# --------------------------------------------------------------------------- modules
@learning_api(["GET", "POST"])
def equipment_modules(request, equipment_id: int):
    eq = _equipment(equipment_id)
    if request.method == "POST":
        return Response(module_out(svc.create_module(eq, request.user, request.data), request.user), status=201)
    mods = list(svc.visible_modules(request.user, eq))
    prog = {p.module_id: p for p in ModuleProgress.objects.filter(user=request.user, module__in=mods)}
    setting = svc.setting_for(eq)
    incomplete = [m.title for m in mods if m.is_published and m.is_required and not svc.is_complete(m, prog.get(m.pk))]
    return Response({
        "equipment": {"equipment_id": eq.pk, "name": eq.name, "code": eq.code},
        "results": [module_out(m, request.user, progress=prog.get(m.pk)) for m in mods],
        "required_incomplete": incomplete,
        "modules_before_training_request": setting.modules_before_training_request,
        "can_edit": svc.is_staff_for(request.user, eq.pk),
        "can_configure": svc.can_configure(request.user, eq.pk),
    })


@learning_api(["GET", "PUT"])
def equipment_setting(request, equipment_id: int):
    eq = _equipment(equipment_id)
    if request.method == "PUT":
        svc.save_setting(eq, request.user, request.data)
    return Response({"modules_before_training_request": svc.setting_for(eq).modules_before_training_request,
                     "can_configure": svc.can_configure(request.user, eq.pk)})


@learning_api(["GET"])
def equipment_progress(request, equipment_id: int):
    """Staff: who has done the online modules (to see readiness before hands-on training)."""
    from iic_booking.users.display import get_user_display_name

    eq = _equipment(equipment_id)
    svc.require(svc.is_staff_for(request.user, eq.pk))
    mods = list(LearningModule.objects.filter(equipment=eq, is_published=True))
    rows: dict[int, dict] = {}
    for p in ModuleProgress.objects.filter(module__in=mods).select_related("user", "module"):
        r = rows.setdefault(p.user_id, {"user": {"id": p.user_id, "name": get_user_display_name(p.user)}, "modules": {}})
        r["modules"][p.module_id] = progress_out(p.module, p)
    required = [m.pk for m in mods if m.is_required]
    for r in rows.values():
        r["required_complete"] = all(r["modules"].get(mid, {}).get("complete") for mid in required)
    return Response({"modules": [{"id": m.pk, "title": m.title, "is_required": m.is_required} for m in mods],
                     "results": sorted(rows.values(), key=lambda r: r["user"]["name"].lower())})


@learning_api(["GET", "PATCH"])
def module_detail(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    if request.method == "PATCH":
        m = svc.update_module(m, request.user, request.data)
    else:
        svc.require(m.is_published or svc.is_staff_for(request.user, m.equipment_id))
    p = ModuleProgress.objects.filter(user=request.user, module=m).first()
    return Response(module_out(m, request.user, detail=True, progress=p))


@learning_api(["POST"], parsers=[MultiPartParser, FormParser])
def module_document(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    m = svc.attach_document(m, request.user, request.FILES.get("file"))
    return Response(module_out(m, request.user, detail=True))


@learning_api(["POST"])
def module_questions(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    return Response(question_out(svc.add_question(m, request.user, request.data), True), status=201)


@learning_api(["PATCH"])
def question_detail(request, pk: int):
    q = get_object_or_404(QuizQuestion.objects.select_related("module"), pk=pk)
    return Response(question_out(svc.update_question(q, request.user, request.data), True))


@learning_api(["POST"])
def module_viewed(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    return Response(progress_out(m, svc.mark_viewed(m, request.user)))


@learning_api(["POST"])
def module_acknowledge(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    return Response(progress_out(m, svc.acknowledge(m, request.user)))


@learning_api(["POST"])
def module_attempt(request, pk: int):
    m = get_object_or_404(LearningModule, pk=pk)
    return Response(svc.submit_quiz(m, request.user, request.data.get("answers") or {}))


@learning_api(["GET"])
def my_progress(request):
    rows = ModuleProgress.objects.filter(user=request.user).select_related("module", "module__equipment")
    return Response({"results": [
        {"module_id": p.module_id, "title": p.module.title,
         "equipment": {"equipment_id": p.module.equipment_id, "name": p.module.equipment.name},
         **progress_out(p.module, p)}
        for p in rows
    ]})


# --------------------------------------------------------------------------- logbook
@learning_api(["GET", "POST"])
def booking_logbook(request, booking_id: int):
    from iic_booking.equipment.models import Booking

    booking = get_object_or_404(Booking.objects.select_related("equipment"), pk=booking_id)
    svc.require(svc.can_use_logbook(request.user, booking), "Only the person who booked or the equipment's staff can see this logbook.")
    if request.method == "POST":
        return Response(entry_out(svc.add_entry(booking, request.user, request.data)), status=201)
    rows = LogbookEntry.objects.filter(booking=booking).select_related("author")
    return Response({"results": [entry_out(e) for e in rows],
                     "kinds": [{"value": v, "label": str(lbl)} for v, lbl in LogbookKind.choices]})


@learning_api(["GET"])
def equipment_logbook(request, equipment_id: int):
    eq = _equipment(equipment_id)
    svc.require(svc.is_staff_for(request.user, eq.pk), "Only the equipment's staff can see its logbook.")
    qs = LogbookEntry.objects.filter(equipment=eq).select_related("author").order_by("-created_at")
    kind = request.query_params.get("kind")
    if kind in LogbookKind.values:
        qs = qs.filter(kind=kind)
    return Response({"results": [entry_out(e) for e in qs[:300]]})
