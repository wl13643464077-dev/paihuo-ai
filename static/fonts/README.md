# 宣传页标题字体

`paihuo-display.woff2` 是 **得意黑 Smiley Sans v2.0.1**（© 2022–2024 atelierAnchor，<https://github.com/atelier-anchor/smiley-sans>）的网页子集，只包含宣传页用到的字符。

- 许可：SIL Open Font License 1.1，见 `paihuo-display.OFL.txt`。允许免费商用与网页嵌入。
- “Smiley / 得意黑”是保留字体名，按 OFL 要求，子集（修改版）已更名为 **PaiHuo Display**。
- 宣传页文案改动后需重新生成子集：取页面可见文字，用 `pyftsubset SmileySans-Oblique.ttf --text-file=chars.txt --flavor=woff2 --layout-features='*'`，再把 name 表改为 PaiHuo Display，并更新 `promo.html` 里的 `?v=`。缺字会自动回退到系统字体。
