from __future__ import annotations

import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

import app as accounting


class FakeStorage:
    def __init__(self, filename: str, payload: bytes = b"OFX"):
        self.filename = filename
        self.payload = payload

    def save(self, destination: Path) -> None:
        destination.write_bytes(self.payload)


@pytest.fixture()
def sqlite_app(tmp_path, monkeypatch):
    monkeypatch.setattr(accounting, "INSTANCE_DIR", tmp_path / "instance")
    monkeypatch.setattr(accounting, "UPLOAD_DIR", tmp_path / "instance" / "uploads")
    monkeypatch.setattr(accounting, "SPEEDPOINT_RUNS_DIR", tmp_path / "instance" / "uploads" / "speedpoint_runs")
    monkeypatch.setattr(accounting, "DB_PATH", tmp_path / "instance" / "accounting.db")
    monkeypatch.setattr(accounting, "ACCOUNT_NAMES_PATH", tmp_path / "missing.xlsx")
    accounting.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    with accounting.app.app_context():
        accounting.initialize_storage()
        yield tmp_path
        if accounting.g.get("db") is not None:
            accounting.g.db.close()
            accounting.g.pop("db", None)


def test_parse_money_is_decimal_and_rejects_non_finite_values():
    assert accounting.parse_money("-12.345") == Decimal("-12.35")
    assert accounting.parse_money("NaN") is None
    assert accounting.parse_money("Infinity") is None
    assert accounting.parse_money("-Infinity") is None


def test_fixed_entry_date_validation():
    assert accounting.valid_entry_date("2026-02-28") == "2026-02-28"
    assert accounting.valid_entry_date("2026-02-29") is None
    assert accounting.valid_entry_date("not-a-date") is None


def test_identical_occurrences_in_one_ofx_file_are_retained(sqlite_app, monkeypatch):
    transactions = [
        {"index": 1, "fitid": "A", "description": "SUPPLIER", "amount": Decimal("10.00"), "payment_date": "2026-01-15"},
        {"index": 2, "fitid": "B", "description": "SUPPLIER", "amount": Decimal("10.00"), "payment_date": "2026-01-15"},
    ]
    monkeypatch.setattr(accounting, "parse_ofx_transactions", lambda path: transactions)
    with accounting.app.app_context():
        batch_id = accounting.execute(
            "INSERT INTO import_batches (source_folder, source_type, status) VALUES (?, ?, ?)",
            ("", "eft", "open"),
        ).lastrowid
        file_id, staged_rows, duplicate = accounting.stage_ofx_file(
            batch_id, FakeStorage("statement.ofx"), "statement.ofx"
        )
        assert file_id is not None
        assert staged_rows == 2
        assert duplicate is False
        assert accounting.query_one("SELECT COUNT(*) AS count FROM staged_payments")["count"] == 2


def test_cross_file_occurrence_counts_still_deduplicate_overlapping_exports(sqlite_app, monkeypatch):
    one = [{"index": 1, "description": "SUPPLIER", "amount": Decimal("10.00"), "payment_date": "2026-01-15"}]
    two = [
        {"index": 1, "description": "SUPPLIER", "amount": Decimal("10.00"), "payment_date": "2026-01-15"},
        {"index": 2, "description": "SUPPLIER", "amount": Decimal("10.00"), "payment_date": "2026-01-15"},
    ]
    transaction_sets = iter([one, two])
    monkeypatch.setattr(accounting, "parse_ofx_transactions", lambda path: next(transaction_sets))
    with accounting.app.app_context():
        batch_id = accounting.execute(
            "INSERT INTO import_batches (source_folder, source_type, status) VALUES (?, ?, ?)",
            ("", "eft", "open"),
        ).lastrowid
        accounting.stage_ofx_file(batch_id, FakeStorage("first.ofx", b"one"), "first.ofx")
        _, staged_rows, _ = accounting.stage_ofx_file(batch_id, FakeStorage("second.ofx", b"two"), "second.ofx")
        assert staged_rows == 1
        assert accounting.query_one("SELECT COUNT(*) AS count FROM staged_payments")["count"] == 2


def test_failed_workbook_parse_rolls_back_database_and_file(sqlite_app, monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", "not-a-workbook")
    monkeypatch.setattr(
        accounting,
        "load_workbook",
        lambda *args, **kwargs: (_ for _ in ()).throw(accounting.InvalidFileException("bad workbook")),
    )
    with accounting.app.app_context():
        batch_id = accounting.execute(
            "INSERT INTO import_batches (source_folder, source_type, status) VALUES (?, ?, ?)",
            ("", "cash", "open"),
        ).lastrowid
        with pytest.raises(accounting.UploadProcessingError):
            accounting.stage_workbook(
                batch_id,
                FakeStorage("01 January 2026.xlsx", payload.getvalue()),
                "01 January 2026.xlsx",
                "2026-01-01",
            )
        assert accounting.query_one("SELECT COUNT(*) AS count FROM import_files")["count"] == 0
        assert not list(accounting.UPLOAD_DIR.glob("*.xlsx"))


def test_manifest_rejects_path_traversal(sqlite_app):
    accounting.SPEEDPOINT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    assert accounting.load_speedpoint_manifest("../secret") is None


def test_negative_fixed_amount_is_preserved():
    assert accounting.parse_money("-25.50") == Decimal("-25.50")


def test_negative_fixed_amount_contributes_to_report_adjustments(monkeypatch):
    monkeypatch.setattr(accounting, "report_linked_amounts_for_month", lambda month: {"cost_of_sale_fuel": Decimal("100.00")})
    monkeypatch.setattr(
        accounting,
        "report_fixed_amounts_for_month",
        lambda month: {"rent": Decimal("-25.00"), "water": Decimal("0.00"), "training": Decimal("0.00")},
    )
    totals = accounting.report_totals_for_month("2026-01")
    assert totals["rent"] == Decimal("-25.00")
    assert totals["cost_of_sale_fuel"] == Decimal("125.00")
