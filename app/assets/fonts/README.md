# PDF fonts

The PDF renderer embeds deterministic font subsets so generated reports do not
depend on fonts installed on the host system.

## Noto Sans

- Version: 2.015
- Source: <https://github.com/notofonts/latin-greek-cyrillic/releases/tag/NotoSans-v2.015>
- Files: `NotoSans-{Regular,Bold,Italic,BoldItalic}.ttf` from `full/ttf`
- License: SIL Open Font License 1.1 (`OFL-NotoSans.txt`)

SHA-256:

```text
f5f552c8c5edb61fe6efb824baf4d4de47b1a8689ab4925ff43f7bd6a4ebece5  NotoSans-Regular.ttf
3a08a47daa00cade516425c15c57615aef2fd418ec9811a7b9f465088f92cc05  NotoSans-Bold.ttf
126522ae1bb9cd92120287fc47dfc74ef981e73931d93e52c565fb7e09b2d74a  NotoSans-Italic.ttf
2e34b41a4b9c234b1be7dff6d06cba18811ecb694b41350873edf0ec16a0f0fa  NotoSans-BoldItalic.ttf
```

## Noto Sans SC

- Google Fonts snapshot downloaded on 2026-07-22
- Source commit: `2894aab31764f10f29c421bdfd2340d3b382d384`
- Source: <https://github.com/google/fonts/tree/2894aab31764f10f29c421bdfd2340d3b382d384/ofl/notosanssc>
- File: `NotoSansSC[wght].ttf`, stored as `NotoSansSC-Variable.ttf`
- License: SIL Open Font License 1.1 (`OFL-NotoSansSC.txt`)

SHA-256:

```text
a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da  NotoSansSC-Variable.ttf
```

Characters missing from both fonts are rendered as an explicit ASCII Unicode
code point (for example `[U+1F600]`) rather than as an invisible `.notdef`
glyph. This preserves diagnostic meaning without claiming full shaping support
for every writing system.
