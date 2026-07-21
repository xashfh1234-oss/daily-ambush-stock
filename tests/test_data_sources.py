from stock_assistant.data_sources import normalize_code


def test_normalize_codes():
    assert normalize_code("000001.SZ") == ("000001", "000001.SZ", "sz.000001")
    assert normalize_code("sh.600000") == ("600000", "600000.SH", "sh.600000")
    assert normalize_code("830001") == ("830001", "830001.BJ", "bj.830001")
    assert normalize_code("920008") == ("920008", "920008.BJ", "bj.920008")
