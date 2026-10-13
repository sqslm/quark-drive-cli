"""Shared strict parser for desktop share input."""
import re
from urllib.parse import urlsplit
from ..errors import UsageError
from ..shares import parse_share_url as parse_share_link
from .share import ShareSource

def parse_share_url(url: str, passcode: str | None = None) -> ShareSource:
    """Pair one typed share link with its optional passcode, or refuse it.

    The link goes through the library's own strict parser, so the window accepts
    exactly what the CLI accepts. An empty passcode stays ``None``: the link's
    own ``pwd`` parameter is then the one that is used, and neither the URL nor
    the passcode reaches the status line or the item log (the source also keeps
    both out of its ``repr``).
    """
    link = parse_share_link(url.strip(), passcode)
    return ShareSource(url=url.strip(), passcode=link.passcode or None)


def parse_share_input(text: str, passcode: str | None = None) -> ShareSource:
    """Extract one Quark URL and code from a pasted link or share message."""
    candidates = re.findall(r'https?://[^\s<>"\'，。；、（）【】「」]+', text, re.IGNORECASE)
    try:
        urls = [url.rstrip(".,;:!?)]}") for url in candidates if urlsplit(url).hostname == "pan.quark.cn"]
    except ValueError:
        raise UsageError("分享链接格式无效") from None
    if len(urls) != 1:
        raise UsageError("请输入单个夸克分享链接或分享文案")
    linked = parse_share_url(urls[0])
    codes = set(re.findall(r"(?:提取码|提取碼|密码)\s*[:：]?\s*([A-Za-z0-9]+)", text))
    if linked.passcode:
        codes.add(linked.passcode)
    if not passcode and len(codes) > 1:
        raise UsageError("分享文案中的提取码不一致，请手动填写正确提取码")
    return parse_share_url(urls[0], passcode or next(iter(codes), None))


