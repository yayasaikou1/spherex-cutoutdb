from __future__ import annotations

from pathlib import Path

from spherex_cutoutdb.irsa_sia import read_votable_bytes
from spherex_cutoutdb.irsa_sia import normalize_sia_dataframe, read_votable_file


def test_sia_parser_normalizes_mock_response():
    path = Path(__file__).parent / "data" / "mock_sia_response.xml"
    df = read_votable_file(path)
    rows = normalize_sia_dataframe(df, source_id="M101", collection="spherex_qr2")
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["source_id"] == "M101"
    assert row["access_url"].startswith("https://irsa.ipac.caltech.edu")
    assert row["parent_filename"].endswith(".fits")
    assert row["detector_id"] == 3
    assert row["product_signature"]
    assert "s3" in row["cloud_access_json"]


def test_sia_parser_prefers_field_names_over_col_ids():
    xml = b"""<?xml version="1.0"?>
<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
  <RESOURCE>
    <TABLE>
      <FIELD ID="col_0" name="s_ra" datatype="double"/>
      <FIELD ID="col_1" name="s_dec" datatype="double"/>
      <FIELD ID="col_15" name="access_url" datatype="char" arraysize="*"/>
      <FIELD ID="col_16" name="access_format" datatype="char" arraysize="*"/>
      <FIELD ID="col_43" name="obs_publisher_did" datatype="char" arraysize="*"/>
      <DATA><TABLEDATA><TR>
        <TD>1.0</TD><TD>2.0</TD>
        <TD>https://irsa.ipac.caltech.edu/ibe/data/spherex/qr2/level2/2025W49_2A/l2b-v20-2025-342/3/level2_2025W49_2A_0478_2D3_spx_l2b-v20-2025-342.fits</TD>
        <TD>image/fits</TD><TD>ivo://example</TD>
      </TR></TABLEDATA></DATA>
    </TABLE>
  </RESOURCE>
</VOTABLE>
"""
    df = read_votable_bytes(xml)
    assert "access_url" in df.columns
    assert "col_15" not in df.columns
    rows = normalize_sia_dataframe(df, source_id="SRC", collection="spherex_qr2")
    assert rows.iloc[0]["access_url"].startswith("https://irsa.ipac.caltech.edu")
    assert rows.iloc[0]["detector_id"] == 3
