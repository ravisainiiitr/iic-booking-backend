"""Research Copilot pilot: equipment manuals, manual answers, slot parity, prepare/file endpoints."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentStatus
from iic_booking.research_copilot.models import (
    DocumentStatus,
    IndexStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    SecurityLevel,
)
from iic_booking.research_copilot.services import manuals as manual_svc
from iic_booking.research_copilot.services import pdf_extract
from iic_booking.research_copilot.services import rag as rag_svc
from iic_booking.research_copilot.services.ingestion import split_pages
from iic_booking.research_copilot.services.v2 import read_tools
from iic_booking.research_copilot.services.v2 import slot_availability as slots_svc
from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.users.models.user_type import UserType

User = get_user_model()

PAGE_1 = (
    "Startup procedure. Switch on the rotary vacuum pump and wait until the chamber vacuum reaches "
    "five times ten to the minus five mbar before opening the gun valve. Never vent the chamber while "
    "the high voltage is on."
)
PAGE_2 = (
    "Sample preparation. Mount dry samples on aluminium stubs with carbon tape. Non conductive samples "
    "must be gold sputter coated for sixty seconds. Maximum sample height is ten millimetres."
)


def _make_pdf(pages: list[str]) -> bytes:
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        cid = 5 + 2 * i
        objs.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>"
            ).encode()
        )
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 8 Tf 20 750 Td ({esc}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _user(email: str, user_type=UserType.STUDENT, **extra):
    return User.objects.create_user(
        email=email,
        password="test-pass-12345",
        user_type=user_type,
        name=email.split("@")[0],
        is_active=True,
        email_verified=True,
        admin_approved=True,
        **extra,
    )


def _client(user) -> APIClient:
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _equipment(name="Zeta Probe Analyzer", code="ZPA-01") -> Equipment:
    from iic_booking.users.models import Department

    dept, _ = Department.objects.get_or_create(
        code="CPM",
        defaults={"name": "Copilot Manuals Dept", "equipment_booking_enabled": True, "equipment_visibility_enabled": True},
    )
    return Equipment.objects.create(
        name=name, code=code, status=EquipmentStatus.ACTIVE, internal_department=dept
    )


def _indexed_manual(equipment, *, pdf: bytes | None = None, security=SecurityLevel.AUTHENTICATED):
    data = pdf or _make_pdf([PAGE_1, PAGE_2])
    with patch.object(manual_svc, "storage_configured", return_value=True), patch.object(
        manual_svc, "_put_object"
    ), patch.object(manual_svc, "dispatch_processing"):
        doc, duplicate = manual_svc.upload_manual(
            data=data, filename="zpa manual.pdf", equipment=equipment, security_level=security
        )
    assert duplicate is False
    with patch.object(manual_svc, "_get_object", return_value=data):
        result = manual_svc.process_manual(doc.id)
    assert result["ok"], result
    doc.refresh_from_db()
    return doc


@pytest.fixture(autouse=True)
def _local_embeddings(settings):
    settings.RESEARCH_COPILOT_EMBEDDING_PROVIDER = "local"


# --- PDF validation / extraction ---------------------------------------------------------------


class TestPdfExtract:
    def test_rejects_non_pdf(self):
        with pytest.raises(pdf_extract.PdfRejected) as exc:
            pdf_extract.validate_pdf(b"hello, this is not a pdf" * 10)
        assert exc.value.code == "NOT_A_PDF"

    @override_settings(RESEARCH_COPILOT_MANUAL_MAX_BYTES=200)
    def test_rejects_oversize(self):
        with pytest.raises(pdf_extract.PdfRejected) as exc:
            pdf_extract.validate_pdf(_make_pdf([PAGE_1, PAGE_2]))
        assert exc.value.code == "FILE_TOO_LARGE"

    def test_extracts_text_per_page(self):
        text = pdf_extract.extract_pages(_make_pdf([PAGE_1, PAGE_2]))
        assert text.page_count == 2
        assert "rotary vacuum pump" in text.pages[0]
        assert "gold sputter" in text.pages[1]

    def test_scanned_or_empty_pdf_is_rejected(self):
        with pytest.raises(pdf_extract.PdfRejected) as exc:
            pdf_extract.extract_pages(_make_pdf(["x", "y"]))
        assert exc.value.code == "NO_EXTRACTABLE_TEXT"


@pytest.mark.django_db
def test_create_conversation_fills_not_null_access_columns():
    from iic_booking.research_copilot.services.conversation import create_conversation

    conv = create_conversation(user=_user("conv-defaults@example.com"), title="t")
    conv.refresh_from_db()
    assert conv.access_mode == "authenticated"
    assert conv.anonymous_session_key == ""


@pytest.mark.django_db
def test_explicit_equipment_code_resolves_among_variants():
    from iic_booking.research_copilot.services.v2.equipment_resolver import resolve_equipment

    user = _user("variants@example.com")
    a = _equipment("Powder X-Ray Diffractometer (PXRD) [A]", "PXRD [A]")
    _equipment("Powder X-Ray Diffractometer (PXRD) [B]", "PXRD [B]")
    _equipment("Powder X-Ray Diffractometer (PXRD) [C]", "PXRD [C]")

    exact = resolve_equipment(text="Show available slots for PXRD [A] this week", user=user)
    assert exact.equipment_id == a.pk
    assert resolve_equipment(text="Show available slots for PXRD this week", user=user).confidence == "AMBIGUOUS"


def test_split_pages_tracks_page_numbers():
    chunks = split_pages(["a " * 10, "b " * 10], chunk_size=8, overlap=2)
    assert chunks[0][1:] == (1, 1)
    assert chunks[-1][1:] == (2, 2)
    assert any(first == 1 and last == 2 for _, first, last in chunks)


# --- Intent routing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What is the maximum sample height for the FESEM?",
        "Show me the SOP for XRD",
        "How do I prepare my sample for TEM?",
        "What safety precautions apply to the ICP-MS?",
        "Open the FESEM manual",
    ],
)
def test_manual_questions_route_to_equipment_manual(text):
    assert resolve_intent(text).intent == "equipment_manual"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("How much does FESEM cost?", "estimate_cost"),
        ("What is the sample preparation fee for XRD?", "estimate_cost"),
        ("Search available slots for FESEM this week", "search_slots"),
        ("Recharge my wallet", "prepare_recharge"),
    ],
)
def test_non_manual_questions_keep_their_intent(text, expected):
    assert resolve_intent(text).intent == expected


def test_sop_token_does_not_match_inside_words():
    assert resolve_intent("isopropanol cleaning of the holder").intent != "equipment_manual"


# --- Manual upload, indexing and retrieval ------------------------------------------------------


@pytest.mark.django_db
class TestManualIngestion:
    def test_upload_stores_under_research_prefix_and_indexes_with_pages(self):
        eq = _equipment()
        doc = _indexed_manual(eq)
        assert doc.source_file_key.startswith(f"research/copilot-manuals/{eq.pk}/")
        assert doc.status == DocumentStatus.ACTIVE
        assert doc.index_status == IndexStatus.INDEXED
        assert doc.page_count == 2
        chunk = KnowledgeChunk.objects.filter(document=doc).first()
        assert chunk.metadata.get("page") == 1
        assert chunk.metadata.get("equipment_id") == eq.pk

    def test_duplicate_upload_is_detected(self):
        eq = _equipment()
        data = _make_pdf([PAGE_1, PAGE_2])
        doc = _indexed_manual(eq, pdf=data)
        with patch.object(manual_svc, "storage_configured", return_value=True), patch.object(
            manual_svc, "_put_object"
        ) as put, patch.object(manual_svc, "dispatch_processing"):
            again, duplicate = manual_svc.upload_manual(data=data, filename="copy.pdf", equipment=eq)
        assert duplicate is True
        assert again.id == doc.id
        put.assert_not_called()

    def test_upload_rejects_invalid_pdf_before_storage(self):
        eq = _equipment()
        with patch.object(manual_svc, "storage_configured", return_value=True), patch.object(
            manual_svc, "_put_object"
        ) as put:
            with pytest.raises(manual_svc.ManualError) as exc:
                manual_svc.upload_manual(data=b"not a pdf at all" * 20, filename="x.pdf", equipment=eq)
        assert exc.value.code == "NOT_A_PDF"
        put.assert_not_called()

    def test_passages_are_scoped_to_one_equipment(self):
        eq1 = _equipment()
        eq2 = _equipment(name="Omega Spectrometer", code="OMS-02")
        _indexed_manual(eq1)
        other = _make_pdf(
            [
                "Omega spectrometer vacuum pump maintenance. " * 6,
                "Omega detector cooling with liquid nitrogen before use. " * 6,
            ]
        )
        _indexed_manual(eq2, pdf=other)
        passages = rag_svc.manual_passages(query="vacuum pump startup", equipment_id=eq1.pk, role_bucket="student")
        assert passages
        docs_eq1 = set(KnowledgeDocument.objects.filter(equipment_id=eq1.pk).values_list("id", flat=True))
        assert all(p["document_id"] in {str(d) for d in docs_eq1} for p in passages)
        assert passages[0]["page"] == 1

    def test_archived_manual_is_not_retrieved(self):
        eq = _equipment()
        doc = _indexed_manual(eq)
        manual_svc.archive_manual(doc)
        assert rag_svc.manual_passages(query="vacuum pump", equipment_id=eq.pk, role_bucket="student") == []

    def test_admin_only_manual_hidden_from_students(self):
        eq = _equipment()
        _indexed_manual(eq, security=SecurityLevel.ADMIN)
        assert rag_svc.manual_passages(query="vacuum pump", equipment_id=eq.pk, role_bucket="student") == []

    def test_vector_search_ignores_other_embedding_models(self):
        from iic_booking.research_copilot.services.embeddings import get_embedding_provider
        from iic_booking.research_copilot.services.vector_store import get_vector_store

        eq = _equipment()
        _indexed_manual(eq)
        provider = get_embedding_provider()
        vec = provider.embed_query("vacuum pump")
        store = get_vector_store()
        same = store.similarity_search(
            query_vector=vec, allowed_levels={"authenticated"}, department_id=None,
            equipment_id=eq.pk, embedding_model=provider.name, embedding_version=provider.version,
        )
        other = store.similarity_search(
            query_vector=vec, allowed_levels={"authenticated"}, department_id=None,
            equipment_id=eq.pk, embedding_model="nomic-embed-text", embedding_version="nomic-embed-text",
        )
        assert same and other == []


# --- Grounded manual answers --------------------------------------------------------------------


@pytest.mark.django_db
class TestManualAnswer:
    def test_grounded_answer_has_page_citations(self):
        eq = _equipment()
        _indexed_manual(eq)
        student = _user("manual-student@example.com")
        with patch.object(
            read_tools, "_generate_manual_answer", return_value=("Start the rotary pump first and wait for vacuum [1].", "")
        ):
            out = read_tools.equipment_manual_answer(
                user=student, text="How do I start the vacuum pump?", context_equipment_id=eq.pk
            )
        assert out["response_kind"] == "ANSWER"
        assert "[1]" in out["content"] and "**Sources**" in out["content"]
        cite = out["metadata"]["citations"][0]
        assert cite["page"] == 1 and cite["source_type"] == "manual"
        assert cite["has_file"] is True and cite["file_endpoint"].endswith(f"/{cite['document_id']}/file/")
        assert out["metadata"]["llm_used"] is True

    def test_uncited_llm_answer_falls_back_to_passages(self):
        eq = _equipment()
        _indexed_manual(eq)
        with patch.object(read_tools, "_generate_manual_answer", return_value=("Just press the green button.", "")):
            out = read_tools.equipment_manual_answer(user=None, text="How do I start the vacuum pump?", context_equipment_id=eq.pk)
        # Anonymous users see only public manuals; this one is authenticated-only -> no passages.
        assert out["metadata"]["manual_found"] is False
        student = _user("manual-student2@example.com")
        with patch.object(read_tools, "_generate_manual_answer", return_value=("Just press the green button.", "")):
            out = read_tools.equipment_manual_answer(user=student, text="How do I start the vacuum pump?", context_equipment_id=eq.pk)
        assert "green button" not in out["content"]
        assert out["metadata"]["llm_used"] is False
        assert out["metadata"]["llm_skipped"] == "ungrounded"

    def test_not_in_manual_escalates(self):
        eq = _equipment()
        _indexed_manual(eq)
        student = _user("manual-student3@example.com")
        with patch.object(read_tools, "_generate_manual_answer", return_value=(read_tools.MANUAL_NOT_FOUND_MARKER, "")):
            out = read_tools.equipment_manual_answer(user=student, text="What is the vacuum pump warranty?", context_equipment_id=eq.pk)
        assert out["escalate_hint"] is True
        assert out["metadata"]["answer_in_manual"] is False

    def test_no_manual_uses_equipment_profile(self):
        eq = _equipment()
        eq.description = "Zeta potential and particle size analyser."
        eq.save(update_fields=["description"])
        student = _user("manual-student4@example.com")
        out = read_tools.equipment_manual_answer(user=student, text="How do I start the vacuum pump?", context_equipment_id=eq.pk)
        assert "No operating manual" in out["content"]
        assert "Zeta potential" in out["content"]
        assert out["metadata"]["manual_found"] is False


# --- Slot parity with the booking page ----------------------------------------------------------


@pytest.mark.django_db
class TestSlotAvailability:
    def _rows(self):
        future = timezone.now() + timedelta(days=1)
        past = timezone.now() - timedelta(hours=2)

        def row(i, start, **kw):
            base = {
                "id": i,
                "status": "AVAILABLE",
                "date": start.date().isoformat(),
                "start_datetime": start.isoformat(),
                "end_datetime": (start + timedelta(hours=1)).isoformat(),
            }
            base.update(kw)
            return base

        return [
            row(1, future),
            row(2, past),
            row(3, future + timedelta(hours=1), status="BOOKED"),
            row(4, future + timedelta(hours=2), booking=99),
            row(5, future + timedelta(hours=3), available_for_external=False),
            row(6, future + timedelta(hours=4)),
        ]

    def test_filters_match_booking_page_rules(self):
        eq = _equipment()
        with patch.object(slots_svc, "_call_daily_slots_view", return_value=(200, {"slots": self._rows()})), patch.object(
            slots_svc, "_home_department_allowed_ids", return_value={1}
        ):
            lookup = slots_svc.find_bookable_slots(
                user=None, equipment_id=eq.pk, start_date=timezone.localdate(), end_date=timezone.localdate() + timedelta(days=3)
            )
        assert lookup.ok
        assert [r["slot_id"] for r in lookup.rows] == [1]

    def test_hidden_equipment_is_reported(self):
        eq = _equipment()
        with patch.object(slots_svc, "_call_daily_slots_view", return_value=(403, {})):
            lookup = slots_svc.find_bookable_slots(
                user=None, equipment_id=eq.pk, start_date=timezone.localdate(), end_date=timezone.localdate()
            )
        assert not lookup.ok and lookup.error == "EQUIPMENT_NOT_VISIBLE"

    def test_inactive_equipment_offers_no_slots(self):
        eq = _equipment()
        eq.status = EquipmentStatus.REPAIR
        eq.save(update_fields=["status"])
        with patch.object(slots_svc, "_call_daily_slots_view", return_value=(200, {"slots": self._rows()})), patch.object(
            slots_svc, "_home_department_allowed_ids", return_value={1, 6}
        ):
            lookup = slots_svc.find_bookable_slots(
                user=None, equipment_id=eq.pk, start_date=timezone.localdate(), end_date=timezone.localdate() + timedelta(days=3)
            )
        assert lookup.ok and lookup.rows == [] and lookup.bookable_equipment is False


# --- Endpoints ----------------------------------------------------------------------------------


@pytest.mark.django_db
class TestPilotEndpoints:
    @pytest.fixture(autouse=True)
    def _pilot(self, settings):
        settings.RESEARCH_COPILOT_ENABLED = True
        settings.RESEARCH_COPILOT_PILOT_EMAILS = "pilot-student@example.com"

    def test_prepare_returns_envelope_for_pilot(self):
        pilot = _user("pilot-student@example.com")
        prep = {
            "ok": True,
            "action": "CREATE_BOOKING",
            "status": "READY_FOR_CONFIRMATION",
            "proposal_id": "p1",
            "confirmation_token": "t1",
            "executable": True,
            "equipment_id": 7,
            "equipment_name": "Zeta",
            "message": "Confirm to book.",
        }
        with patch(
            "iic_booking.research_copilot.services.v2.mutations.booking.prepare_booking_create", return_value=prep
        ) as mocked:
            res = _client(pilot).post(
                "/api/v1/research-copilot/mutations/prepare/",
                {"action": "CREATE_BOOKING", "equipment_id": 7, "slot_ids": [11]},
                format="json",
            )
        assert res.status_code == 200, res.content
        assert res.data["response"]["cards"][0]["type"] == "booking_proposal"
        assert mocked.call_args.kwargs["slot_ids"] == [11]

    def test_prepare_domain_error_is_200_with_explanation(self):
        pilot = _user("pilot-student@example.com")
        with patch(
            "iic_booking.research_copilot.services.v2.mutations.booking.prepare_booking_create",
            return_value={"ok": False, "error": "SLOT_NOT_BOOKABLE", "message": "That slot was just taken."},
        ):
            res = _client(pilot).post(
                "/api/v1/research-copilot/mutations/prepare/",
                {"action": "CREATE_BOOKING", "equipment_id": 7, "slot_ids": [11]},
                format="json",
            )
        assert res.status_code == 200
        assert res.data["ok"] is False
        assert "just taken" in res.data["response"]["content"]

    def test_prepare_rejects_other_actions_and_non_pilots(self):
        pilot = _user("pilot-student@example.com")
        res = _client(pilot).post(
            "/api/v1/research-copilot/mutations/prepare/",
            {"action": "WALLET_RECHARGE", "equipment_id": 7, "slot_ids": [11]},
            format="json",
        )
        assert res.status_code == 400
        outsider = _user("outsider@example.com")
        res = _client(outsider).post(
            "/api/v1/research-copilot/mutations/prepare/",
            {"action": "CREATE_BOOKING", "equipment_id": 7, "slot_ids": [11]},
            format="json",
        )
        assert res.status_code == 503

    def test_manual_file_link_requires_permission(self):
        eq = _equipment()
        doc = _indexed_manual(eq)
        admin_doc = _indexed_manual(_equipment(name="Omega Spectrometer", code="OMS-02"), security=SecurityLevel.ADMIN)
        pilot = _user("pilot-student@example.com")
        with patch.object(manual_svc, "presigned_url", return_value="https://signed.example/manual.pdf"):
            ok = _client(pilot).get(f"/api/v1/research-copilot/knowledge/documents/{doc.id}/file/")
            hidden = _client(pilot).get(f"/api/v1/research-copilot/knowledge/documents/{admin_doc.id}/file/")
            outsider = _client(_user("outsider@example.com")).get(
                f"/api/v1/research-copilot/knowledge/documents/{doc.id}/file/"
            )
            anon = APIClient().get(f"/api/v1/research-copilot/knowledge/documents/{doc.id}/file/")
        assert ok.status_code == 200 and ok.data["url"].startswith("https://signed.example/")
        assert ok["Cache-Control"] == "private, no-store"
        assert hidden.status_code == 404
        assert outsider.status_code == 503
        assert anon.status_code in (401, 403)

    def test_archived_manual_file_is_not_served(self):
        eq = _equipment()
        doc = _indexed_manual(eq)
        manual_svc.archive_manual(doc)
        pilot = _user("pilot-student@example.com")
        with patch.object(manual_svc, "presigned_url", return_value="https://signed.example/manual.pdf"):
            res = _client(pilot).get(f"/api/v1/research-copilot/knowledge/documents/{doc.id}/file/")
        assert res.status_code == 404
