"""DemoColumns.xlsx headers must map to the canonical field keys.

Regression: OD_NetPremium, TP_NetPremium and TranDated had no alias, so the AI
was asked for od_netpremium / tp_netpremium / trandated without their field
instructions, and the premiums it returned were written to Excel as 0.

Synthetic data only: a fake AI reply, no customer documents, no network.
"""

import json
import re

import openpyxl

import config
from core import policy_engine as pe


def _demo_fields():
    return pe._ensure_extra_fields(pe._load_policy_fields_from_excel(config.DEMO_COLUMNS_WORKBOOK))


def test_every_demo_columns_header_has_an_alias():
    wb = openpyxl.load_workbook(config.DEMO_COLUMNS_WORKBOOK, read_only=True)
    headers = {str(c) for ws in wb.worksheets for c in next(ws.iter_rows(max_row=1, values_only=True)) if c}
    wb.close()
    unmapped = sorted(
        h
        for h in headers
        if pe._normalize_field_key(h) not in pe._POLICY_FIELD_KEY_ALIASES
        and pe._canonical_field_key(h) not in pe._NON_EXTRACTION_KEYS
    )
    assert unmapped == []


def test_premium_and_txn_headers_get_their_instructions():
    fields = _demo_fields()
    keys = {key for key, _ in fields}
    assert {"od_net_premium", "tp_net_premium", "txn_date"} <= keys
    assert not keys & {"od_netpremium", "tp_netpremium", "trandated"}

    prompt = pe.make_extraction_prompt("sample policy text", fields)
    assert "Total own damage premium" in prompt
    assert "Total liability premium" in prompt


def test_ai_premiums_reach_excel(monkeypatch, tmp_path):
    fields = _demo_fields()
    monkeypatch.setattr(pe, "POLICY_FIELDS", fields)

    # The fake model answers every key the prompt asks for, whatever its spelling.
    answers = {"odnetpremium": "1200", "tpnetpremium": "750", "policyno": "TEST-0001"}
    reply = {key: answers.get(re.sub(r"[^a-z0-9]", "", key)) for key, _ in fields}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    def fake_post(url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(pe.requests, "post", fake_post)

    result = pe.run_inference_api(
        api_key="test-key",
        api_base_url="https://example.invalid/v1",
        model_name="fake-model",
        policy_text="Motor policy schedule\nPolicy No TEST-0001\n",
        max_chars=None,
        log_placeholder=None,
    )
    out = pe.append_result_to_excel("sample.pdf", result, output_excel=tmp_path / "out.xlsx")

    ws = openpyxl.load_workbook(out)["Policies"]
    row = dict(zip([c.value for c in ws[1]], [c.value for c in ws[2]], strict=True))
    assert row["OD_NetPremium"] == 1200
    assert row["TP_NetPremium"] == 750
