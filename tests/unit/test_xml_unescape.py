from utils import unescape_xml


def test_unescape_lt_gt():
    assert unescape_xml(b"&lt;a&gt;") == "<a>"


def test_unescape_amp():
    assert "Tom & Jerry" == unescape_xml(b"Tom &amp; Jerry")


def test_unescape_quot():
    assert unescape_xml(b"&quot;hi&quot;") == '"hi"'


def test_unescape_apos():
    assert unescape_xml(b"it&apos;s") == "it's"


def test_passthrough():
    assert unescape_xml(b"plain") == "plain"
