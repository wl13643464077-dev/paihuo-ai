"""公众号排版引擎(V24):markdown → 内联样式 HTML,12 套精品主题.

- 公众号编辑器只认内联 CSS(粘贴/草稿箱 API 都会剥掉 <style> 块),所以所有样式都
  逐标签内联;主题参考 mdnice/135 编辑器的主流审美,每套一个主色 + 独立的小标题造型。
- 站外链接公众号会被剥掉 → 转成「正文角标 + 文末参考链接」(mdnice 同款做法);
- 图片:传入 job 素材图(真实抓取图/AI 图),自动插入到各小节之间;
- 复制/预览用 /pubfile 签名公开链接(公众号编辑器/微信服务器抓图不带登录态)。
"""
import hashlib
import hmac
import html as _htmlmod
import re
from urllib.parse import urlsplit

from markdown import markdown as _md

DEFAULT_THEME = "orange"

# 每套主题:name/emoji/color(主色,前端色卡)+ 各标签模板
# h2 造型是主题灵魂:胶囊/左竖条/编号/下划线/居中括弧/渐变……各不相同
THEMES = {
    "orange": {"name": "橙心暖阳", "emoji": "🍊", "color": "#ff7f2a",
               "h2": ("<section style='margin:28px 0 16px;text-align:left'>"
                      "<span style='display:inline-block;background:linear-gradient(90deg,#ff7f2a,#ffb347);"
                      "color:#fff;font-size:17px;font-weight:700;padding:6px 16px;border-radius:99px'>{text}</span></section>"),
               "strong": "#ff7f2a",
               "quote_css": "border-left:4px solid #ff7f2a;background:#fff7f0;color:#8c5a33;"},
    "ink": {"name": "墨黑极简", "emoji": "🖤", "color": "#111111",
            "h2": ("<section style='margin:30px 0 16px'>"
                   "<span style='display:inline-block;border-left:6px solid #111;padding-left:12px;"
                   "font-size:18px;font-weight:800;color:#111;letter-spacing:1px'>{text}</span></section>"),
            "strong": "#111111",
            "quote_css": "border-left:3px solid #111;background:#f6f6f6;color:#555;"},
    "techblue": {"name": "科技蓝", "emoji": "🔷", "color": "#1e6fff",
                 "h2": ("<section style='margin:28px 0 16px'>"
                        "<span style='font-size:22px;font-weight:800;color:#1e6fff;font-style:italic'>{n:02d}</span>"
                        "<span style='display:inline-block;margin-left:10px;font-size:17px;font-weight:700;color:#222;"
                        "border-bottom:3px solid #1e6fff;padding-bottom:4px'>{text}</span></section>"),
                 "strong": "#1e6fff",
                 "quote_css": "border-left:4px solid #1e6fff;background:#f0f6ff;color:#3a5a8c;"},
    "jade": {"name": "翡翠绿", "emoji": "🌿", "color": "#00b96b",
             "h2": ("<section style='margin:28px 0 16px'>"
                    "<span style='display:inline-block;font-size:17px;font-weight:700;color:#00875a;"
                    "background:linear-gradient(to top,#c9f4dd 40%,transparent 40%);padding:0 6px'>{text}</span></section>"),
             "strong": "#00875a",
             "quote_css": "border-left:4px solid #00b96b;background:#f0fbf5;color:#3c7a5d;"},
    "violet": {"name": "姹紫", "emoji": "💜", "color": "#8a4baf",
               "h2": ("<section style='margin:28px 0 16px;text-align:center'>"
                      "<span style='display:inline-block;background:linear-gradient(90deg,#8a4baf,#c084fc);color:#fff;"
                      "font-size:17px;font-weight:700;padding:7px 24px;border-radius:6px;"
                      "box-shadow:3px 3px 0 #ead6f8'>{text}</span></section>"),
               "strong": "#8a4baf",
               "quote_css": "border-left:4px solid #c084fc;background:#faf5ff;color:#7c5295;"},
    "scarlet": {"name": "红绯热烈", "emoji": "🧧", "color": "#e63946",
                "h2": ("<section style='margin:28px 0 16px'>"
                       "<span style='display:inline-block;background:#fdecee;border-left:5px solid #e63946;"
                       "color:#c1121f;font-size:17px;font-weight:700;padding:7px 14px'>{text}</span></section>"),
                "strong": "#e63946",
                "quote_css": "border-left:4px solid #e63946;background:#fdf0f0;color:#a4494f;"},
    "aqua": {"name": "蔚蓝渐变", "emoji": "🌊", "color": "#00a6fb",
             "h2": ("<section style='margin:28px 0 16px;text-align:center'>"
                    "<span style='display:inline-block;background:linear-gradient(120deg,#4facfe,#00f2fe);color:#fff;"
                    "font-size:17px;font-weight:700;padding:7px 26px;border-radius:99px 4px 99px 4px'>{text}</span></section>"),
             "strong": "#0582ca",
             "quote_css": "border-left:4px solid #4facfe;background:#eef8ff;color:#3c6e91;"},
    "magazine": {"name": "杂志留白", "emoji": "📖", "color": "#c0a062",
                 "serif": True,
                 "h2": ("<section style='margin:34px 0 18px;text-align:center'>"
                        "<span style='font-size:13px;color:#c0a062;letter-spacing:4px'>—&nbsp;&nbsp;</span>"
                        "<span style='font-size:18px;font-weight:700;color:#333;letter-spacing:2px'>{text}</span>"
                        "<span style='font-size:13px;color:#c0a062;letter-spacing:4px'>&nbsp;&nbsp;—</span></section>"),
                 "strong": "#a8863d",
                 "quote_css": "border:none;background:#faf7f0;color:#8c7a55;text-align:center;font-style:italic;"},
    "sakura": {"name": "樱花软糯", "emoji": "🌸", "color": "#ff7eb3",
               "h2": ("<section style='margin:28px 0 16px'>"
                      "<span style='display:inline-block;background:#fff0f6;border:2px dashed #ff7eb3;color:#e05a92;"
                      "font-size:17px;font-weight:700;padding:6px 18px;border-radius:16px'>{text}</span></section>"),
               "strong": "#e05a92",
               "quote_css": "border-left:4px solid #ffb3d1;background:#fff5f9;color:#b06a88;"},
    "gold": {"name": "商务鎏金", "emoji": "🏆", "color": "#b8860b",
             "h2": ("<section style='margin:28px 0 16px'>"
                    "<span style='display:inline-block;background:#1f2430;color:#e6c26e;font-size:17px;"
                    "font-weight:700;padding:7px 18px;border-radius:4px;letter-spacing:1px'>{text}</span></section>"),
             "strong": "#a8781a",
             "quote_css": "border-left:4px solid #b8860b;background:#faf6ec;color:#7d6a3a;"},
    "geek": {"name": "极客终端", "emoji": "💻", "color": "#00c853",
             "h2": ("<section style='margin:28px 0 16px'>"
                    "<span style='font-family:Menlo,Consolas,monospace;color:#00a344;font-size:17px;font-weight:700'>"
                    "<span style='color:#bbb'>##&nbsp;</span>{text}</span>"
                    "<section style='height:2px;background:linear-gradient(90deg,#00c853,transparent);margin-top:6px;max-width:280px'></section></section>"),
             "strong": "#00a344",
             "quote_css": "border-left:4px solid #00c853;background:#f2faf4;color:#4a7a58;"},
    "guochao": {"name": "国风朱砂", "emoji": "🏮", "color": "#c1272d",
                "serif": True,
                "h2": ("<section style='margin:30px 0 16px;text-align:center'>"
                       "<span style='color:#c1272d;font-size:18px'>「</span>"
                       "<span style='font-size:18px;font-weight:700;color:#3a3a3a;letter-spacing:2px'>{text}</span>"
                       "<span style='color:#c1272d;font-size:18px'>」</span></section>"),
                "strong": "#c1272d",
                "quote_css": "border-left:4px solid #c1272d;background:#faf4f0;color:#8c5347;"},
}


def theme_list():
    return [{"key": k, "name": t["name"], "emoji": t["emoji"], "color": t["color"]}
            for k, t in THEMES.items()]


# ---------------- 签名公开图链(公众号编辑器/微信服务器抓图不带登录态) ----------------
def _secret() -> bytes:
    from . import auth
    return auth._secret()


def sign_file(rel_path: str) -> str:
    """"job12/media_v1_0.png" → "/pubfile/<sig>/job12/media_v1_0.png"(签名即凭证)."""
    sig = hmac.new(_secret(), f"pubfile:{rel_path}".encode(), hashlib.sha256).hexdigest()
    return f"/pubfile/{sig}/{rel_path}"


def verify_file(sig: str, rel_path: str) -> bool:
    want = hmac.new(_secret(), f"pubfile:{rel_path}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig or "", want)


# ---------------- 渲染 ----------------
def _base_font(t):
    if t.get("serif"):
        return ("Optima-Regular,PingFangTC-light,'Songti SC',STSong,'Noto Serif CJK SC',serif")
    return ("-apple-system,BlinkMacSystemFont,'Helvetica Neue','PingFang SC',"
            "'Hiragino Sans GB','Microsoft YaHei',sans-serif")


def render(body_md: str, theme_key: str, images: list = None, title: str = "") -> str:
    """正文 markdown → 公众号可粘贴的内联样式 HTML.

    images: [{"url": 公开可抓的图片地址, ...}],自动插到小节之间。
    返回完整 <section> 片段(不含标题——公众号标题是单独字段)。
    """
    t = THEMES.get(theme_key) or THEMES[DEFAULT_THEME]
    accent = t["color"]
    body_md = (body_md or "").strip()
    # 正文若以与标题相同的 H1 开头,去掉(标题单独填,不重复占正文)
    body_md = re.sub(r"^#\s+.+\n+", "", body_md, count=1)
    body_md = re.sub(r"<[^>\n]{1,2000}>", "", body_md)
    # Python-Markdown 默认保留 raw HTML；先转义用户原文，避免公众号预览和
    # 草稿箱拿到 script/事件属性。Markdown 自身生成的有限标签仍正常工作。
    html = _md(
        _htmlmod.escape(body_md, quote=False),
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    # 正文里的 Markdown 图片不允许自行指定 URL；素材图片只能由下方
    # _weave_images() 使用服务端签名后的可信地址插入。
    html = re.sub(r"<img\b[^>]*?/?>", "", html, flags=re.I | re.S)

    # 站外链接 → 角标 + 文末参考(公众号会剥站外 <a>)
    refs = []

    def _link(m):
        href, text = m.group(1), m.group(2)
        parsed = urlsplit(_htmlmod.unescape(href))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return text
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and (
                parsed.hostname == "mp.weixin.qq.com"
                or parsed.hostname.endswith(".mp.weixin.qq.com")
            )
        ):
            return f"<a href='{href}' style='color:{accent};text-decoration:none'>{text}</a>"
        refs.append((re.sub(r"<[^>]+>", "", text), href))
        return (f"<span style='color:{accent}'>{text}"
                f"<sup style='font-size:11px'>[{len(refs)}]</sup></span>")

    html = re.sub(r"<a[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", _link, html, flags=re.S)

    # 素材图插入:每 1-2 个小节(或每 3 段)插一张
    html = _weave_images(html, [im.get("url") for im in (images or []) if im.get("url")])

    # ---- 逐标签内联样式 ----
    p_css = ("margin:0 0 18px;font-size:15.5px;color:#3a3a3a;line-height:1.85;"
             "letter-spacing:.6px;text-align:justify")
    h2_counter = [0]

    def _h2(m):
        h2_counter[0] += 1
        text = m.group(1).strip()
        return t["h2"].format(text=text, n=h2_counter[0])

    html = re.sub(r"<h1[^>]*>(.*?)</h1>", _h2, html, flags=re.S)
    html = re.sub(r"<h2[^>]*>(.*?)</h2>", _h2, html, flags=re.S)
    html = re.sub(r"<h3[^>]*>(.*?)</h3>",
                  rf"<section style='margin:22px 0 12px'><span style='font-size:16px;font-weight:700;"
                  rf"color:{accent}'>◆ \1</span></section>", html, flags=re.S)
    html = re.sub(r"<h[46][^>]*>(.*?)</h[46]>",
                  r"<section style='margin:18px 0 10px;font-size:15.5px;font-weight:700;color:#333'>\1</section>",
                  html, flags=re.S)
    html = re.sub(r"<blockquote>",
                  f"<blockquote style='margin:20px 0;padding:14px 16px;border-radius:4px;"
                  f"font-size:14.5px;line-height:1.8;{t['quote_css']}'>", html)
    html = re.sub(r"<strong>", f"<strong style='color:{t['strong']}'>", html)
    html = re.sub(r"<em>", "<em style='color:#888'>", html)
    html = re.sub(r"<ul>", "<ul style='margin:0 0 18px;padding-left:22px'>", html)
    html = re.sub(r"<ol>", "<ol style='margin:0 0 18px;padding-left:24px'>", html)
    html = re.sub(r"<li>",
                  "<li style='margin:6px 0;font-size:15.5px;color:#3a3a3a;line-height:1.8;letter-spacing:.5px'>",
                  html)
    html = re.sub(r"<pre><code[^>]*>",
                  "<pre style='background:#282c34;color:#abb2bf;padding:14px;border-radius:6px;overflow-x:auto;"
                  "font-size:13px;line-height:1.6;margin:0 0 18px'><code style='font-family:Menlo,Consolas,monospace'>",
                  html)
    html = re.sub(r"<code>",
                  f"<code style='background:rgba(27,31,35,.05);color:{accent};padding:2px 5px;"
                  f"border-radius:3px;font-size:13.5px;font-family:Menlo,Consolas,monospace'>", html)
    html = re.sub(r"<hr\s*/?>",
                  f"<section style='margin:26px 0;height:1px;"
                  f"background:linear-gradient(90deg,transparent,{accent},transparent)'></section>", html)
    html = re.sub(r"<table>",
                  "<table style='border-collapse:collapse;margin:0 0 18px;width:100%;font-size:14px'>", html)
    html = re.sub(r"<th>", f"<th style='border:1px solid #ddd;padding:8px;background:{accent};"
                           f"color:#fff;font-weight:700'>", html)
    html = re.sub(r"<td>", "<td style='border:1px solid #ddd;padding:8px;color:#3a3a3a'>", html)
    html = re.sub(r"<img ([^>]*?)/?>",
                  r"<img \1 style='max-width:100%;border-radius:8px;display:block;margin:0 auto'/>", html)
    html = re.sub(r"<p>", f"<p style='{p_css}'>", html)

    # 参考链接
    if refs:
        items = "".join(
            f"<section style='margin:4px 0;font-size:12.5px;color:#999;word-break:break-all'>"
            f"[{i + 1}] {_htmlmod.escape(txt)}:{_htmlmod.escape(url)}</section>"
            for i, (txt, url) in enumerate(refs))
        html += (f"<section style='margin-top:26px;padding-top:12px;border-top:1px dashed #ddd'>"
                 f"<section style='font-size:13px;color:#888;font-weight:700;margin-bottom:6px'>参考链接</section>"
                 f"{items}</section>")

    # 主题化收尾
    html += (f"<section style='text-align:center;margin:34px 0 8px'>"
             f"<span style='display:inline-block;font-size:13px;color:{accent};font-weight:700;"
             f"letter-spacing:6px'>· END ·</span>"
             f"<section style='font-size:12.5px;color:#bbb;margin-top:10px'>"
             f"喜欢这篇的话,点个「赞」和「在看」再走吧&nbsp;👇</section></section>")

    return (f"<section style=\"font-family:{_base_font(t)};padding:4px 2px;background:#fff;\" "
            f"data-paihuo-theme='{theme_key}'>{html}</section>")


def _weave_images(html: str, urls: list) -> str:
    """把素材图织进正文:优先跟在每个小节标题后的第一段末尾,不够的均匀分布."""
    if not urls:
        return html
    fig = ("<section style='margin:8px 0 20px;text-align:center'>"
           "<img src='{u}'/></section>")   # style 由后续统一内联步骤加
    paras = html.split("</p>")
    if len(paras) <= 1:
        return html + "".join(fig.format(u=u) for u in urls)
    n_slots = len(paras) - 1                      # 末尾一段之后不算插槽
    step = max(2, round(n_slots / (len(urls)))) if len(urls) else n_slots
    out, used = [], 0
    for i, seg in enumerate(paras):
        out.append(seg)
        if i < len(paras) - 1:
            out.append("</p>")
            if used < len(urls) and (i + 1) % step == 0:
                out.append(fig.format(u=urls[used]))
                used += 1
    while used < len(urls):                        # 剩余的贴在文末
        out.append(fig.format(u=urls[used]))
        used += 1
    return "".join(out)
