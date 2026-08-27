"""Tests for profile derivation from a publisher template.

These use the two templates in `templates/`, because the interesting failure
mode is not "does the regex work" but "does it read a real publisher file".
Each assertion is a value that can be checked by opening the template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retypeset import template_profile as tp
from retypeset.profile import JournalProfile, load_profiles

ROOT = Path(__file__).resolve().parent.parent
IEEE = ROOT / "templates" / "ieee_template.docx"
ELECTRICA = ROOT / "templates" / "ELECTRICA Manuscript Template-OTH.docx"


@pytest.mark.skipif(not IEEE.exists(), reason="IEEE template not present")
def test_ieee_template_presentation_is_read_not_guessed():
    d = tp.derive(IEEE)
    assert d.profile.docx.columns == 2                 # two-column body
    assert d.profile.docx.body_font == "Times New Roman"
    assert d.profile.docx.margins_mm["left"] > 0
    assert d.profile.verified is False                 # never claim otherwise
    assert d.profile.docx.template_file.endswith("ieee_template.docx")
    assert any(e.startswith("read") for e in d.evidence)


@pytest.mark.skipif(not IEEE.exists(), reason="IEEE template not present")
def test_ieee_template_mines_its_own_instructions():
    d = tp.derive(IEEE)
    # "The abstract must be between 150-250 words." -- upper bound is the limit.
    assert d.profile.structure.abstract_max_words == 250
    assert d.profile.references.style == "numeric"
    assert any(e.startswith("mined") for e in d.evidence)


@pytest.mark.skipif(not ELECTRICA.exists(), reason="ELECTRICA template not present")
def test_electrica_template_spelled_numbers_and_lookback():
    d = tp.derive(ELECTRICA)
    # "(250 words max.)" sits three sentences below the ABSTRACT heading, and
    # the keyword range is spelled out: "a minimum of three to a maximum of six".
    assert d.profile.structure.abstract_max_words == 250
    assert (d.profile.structure.keywords_min,
            d.profile.structure.keywords_max) == (3, 6)


@pytest.mark.skipif(not IEEE.exists(), reason="IEEE template not present")
def test_seeding_keeps_base_values_the_template_does_not_prove():
    base = load_profiles()["elsevier_generic"]
    d = tp.derive(IEEE, base=base)
    # The template says nothing about highlights, so the seed survives.
    assert (d.profile.structure.highlights_required
            == base.structure.highlights_required)
    # ...but it does prove two columns, and that must override the seed.
    assert d.profile.docx.columns == 2


@pytest.mark.skipif(not IEEE.exists(), reason="IEEE template not present")
def test_derived_profile_round_trips_through_the_loader(tmp_path):
    d = tp.derive(IEEE, profile_id="unit_test_tpl")
    path = tp.save(d.profile, tmp_path, overwrite=True)
    assert path.exists()
    loaded = load_profiles(tmp_path)
    assert "unit_test_tpl" in loaded
    assert isinstance(loaded["unit_test_tpl"], JournalProfile)
    assert loaded["unit_test_tpl"].docx.columns == 2


def test_no_limit_is_invented_from_an_empty_document(tmp_path):
    """A template with no instructions must produce no structural limits.

    An invented limit is worse than a missing one: it fires as a finding the
    author cannot trace to anything.
    """
    import zipfile
    p = tmp_path / "empty.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
                   'openxmlformats.org/wordprocessingml/2006/main"><w:body/>'
                   '</w:document>')
    d = tp.derive(p)
    s = d.profile.structure
    assert s.abstract_max_words is None
    assert s.keywords_max is None
    assert s.highlights_required is False
