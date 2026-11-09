import pandas as pd
import pytest

from transaction_ml.ingestion.excel_parser import clean_amount, parse_bank_statement
from transaction_ml.ingestion.upi_parser import parse_upi_string


def test_sbi_style_headers_detect_credit_column(write_csv):
    # "Description" contains the letters "cr"; substring matching used to pick it as the credit column.
    path = write_csv([
        ["Txn Date", "Description", "Debit", "Credit", "Balance"],
        ["05/03/2026", "SALARY ACME", "", "85,000.00", "86000"],
        ["06/03/2026", "UPI/DR/111/SWIGGY/HDFC/swiggy@hdfc/food", "450.00", "", "85550"],
    ])
    salary, swiggy = parse_bank_statement(path)
    assert salary.amount == 85000.0
    assert swiggy.amount == -450.0


def test_dates_are_day_first(write_csv):
    path = write_csv([["Date", "Narration", "Withdrawal Amt", "Deposit Amt"], ["05/03/2026", "X", "1", ""]])
    (t,) = parse_bank_statement(path)
    assert (t.date.year, t.date.month, t.date.day) == (2026, 3, 5)


def test_identical_same_day_rows_get_distinct_ids(write_csv):
    path = write_csv([
        ["Date", "Narration", "Withdrawal Amt", "Deposit Amt"],
        ["01/04/2026", "CHAI POINT", "20", ""],
        ["01/04/2026", "CHAI POINT", "20", ""],
    ])
    a, b = parse_bank_statement(path)
    assert a.id != b.id
    # ...and stay stable across re-parses
    assert [t.id for t in parse_bank_statement(path)] == [a.id, b.id]


def test_preamble_lines_and_icici_remarks_column(write_csv):
    path = write_csv([
        ["Account Number: XXXX1234"],
        ["Statement for March 2026"],
        [],
        ["S No.", "Value Date", "Transaction Date", "Transaction Remarks", "Withdrawal Amount (INR )", "Deposit Amount (INR )", "Balance (INR )"],
        ["1", "02/03/2026", "01/03/2026", "AMAZON PAY", "1,299.00", "0", "5000"],
        ["", "", "", "Opening balance + total", "", "", ""],
    ])
    (t,) = parse_bank_statement(path)
    assert t.merchant_name == "AMAZON PAY"
    assert t.amount == -1299.0
    assert t.date.day == 1  # transaction date, not value date


def test_single_amount_column_with_dr_cr_indicator(write_csv):
    path = write_csv([
        ["Date", "Particulars", "Amount", "Dr / Cr"],
        ["01/03/2026", "REFUND", "500", "CR"],
        ["02/03/2026", "RENT", "15000", "DR"],
    ])
    refund, rent = parse_bank_statement(path)
    assert refund.amount == 500.0
    assert rent.amount == -15000.0


def test_xlsx_input(tmp_path):
    path = str(tmp_path / "s.xlsx")
    pd.DataFrame({"Date": ["01/03/2026"], "Narration": ["NETFLIX"], "Withdrawal Amt": [649], "Deposit Amt": [None]}).to_excel(path, index=False)
    (t,) = parse_bank_statement(path)
    assert (t.merchant_name, t.amount) == ("NETFLIX", -649.0)


def test_unrecognised_layout_raises(write_csv):
    with pytest.raises(ValueError):
        parse_bank_statement(write_csv([["foo", "bar"], ["1", "2"]]))


@pytest.mark.parametrize("raw,expected", [("1,234.50", 1234.5), ("₹ 99", 99.0), ("200.00 Cr", 200.0), ("", 0.0), (None, 0.0), (12, 12.0)])
def test_clean_amount(raw, expected):
    assert clean_amount(raw) == expected


def test_upi_string_parsing():
    upi = parse_upi_string("UPI/DR/412345678901/ZOMATO LTD/HDFC/zomato@hdfcbank/dinner")
    assert (upi.type, upi.merchant_or_person, upi.bank, upi.handle_or_gateway, upi.note) == \
        ("DR", "ZOMATO LTD", "HDFC", "zomato@hdfcbank", "dinner")
    assert parse_upi_string("NEFT-ACME SALARY") is None
