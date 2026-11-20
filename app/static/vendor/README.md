# Vendored front-end libraries (Notes tab)

Served locally by `dashboard.py` from `/static/vendor/…` — no CDN, so the
dashboard works offline and nothing leaves the machine. Copied unmodified from
the npm registry tarballs on 2026-11-20.

| Library | Version | Files | License |
|---|---|---|---|
| [marked](https://github.com/markedjs/marked) | 12.0.2 | `marked/marked.min.js` | MIT — `marked/LICENSE.md` |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.1.6 | `dompurify/purify.min.js` | Apache-2.0 or MPL-2.0 — `dompurify/LICENSE` |
| [KaTeX](https://github.com/KaTeX/KaTeX) | 0.16.11 | `katex/katex.min.{js,css}`, `katex/contrib/auto-render.min.js`, `katex/fonts/*.woff2` | MIT — `katex/LICENSE` |

To upgrade: download `https://registry.npmjs.org/<name>/-/<name>-<version>.tgz`,
copy the same files, update this table.
