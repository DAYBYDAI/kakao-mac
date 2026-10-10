# Third-party notices

## Adapted code

`skill/kakao-mac/scripts/kakao_mac.py` is adapted from `kakaotalk-mac/scripts/kakaotalk_mac.py` in [NomaDamas/k-skill](https://github.com/NomaDamas/k-skill), commit c9116b555fa1177f0673e346243b27f1af7e8a87 (2026-04-17), MIT license. The upstream license file carries the notice `Copyright (c) 2026`. The full MIT permission notice is in LICENSE and applies to the redistributed portions. The upstream project later replaced this helper with a different tool, so the file no longer exists on its main branch.

Changes made here: a single data folder (`~/.kakao-mac`), an owner-only key cache written atomically, no output format that prints the key, a check that the cached account is still one KakaoTalk lists as signed in, a `rooms` command that resolves real room titles, a fallback path for a hand-built `kakaocli`, clearer errors for missing permissions, and a progress note before the account-id search.

`skill/kakao-mac/scripts/kakao_attachments.py`, `skill/kakao-mac/SKILL.md`, `skill/kakao-mac/references/send.md`, `AGENTS.md` and `install.sh` are original to this repository.

## Runtime dependency (not bundled)

[kakaocli](https://github.com/silver-flight-group/kakaocli), copyright (c) 2026 Silver Flight Group, LLC, MIT license. The recipient installs it separately, either with Homebrew (`silver-flight-group/tap/kakaocli`, built from source) or by building tag v0.6.0. It links [SQLCipher](https://github.com/sqlcipher/sqlcipher) (BSD-style license), which Homebrew installs. No kakaocli source or binary is included in this repository.

## Optional tools (not bundled)

- `pdftotext` from [Poppler](https://poppler.freedesktop.org/) (GPL), used as a separate program to extract PDF text when the recipient has installed it.
- [kordoc](https://www.npmjs.com/package/kordoc) (MIT), used as a separate program to extract HWP text when the recipient has installed it.
- `curl`, `textutil`, `plutil`, `ioreg`, `pbcopy` and Python 3 are part of macOS or Apple's Command Line Tools.

## Trademarks and affiliation

This is an unofficial community tool. It is not made, supported, sponsored or approved by Kakao or Anthropic. "KakaoTalk" and "카카오톡" are used only to name the application this tool works with, and belong to their owner.
