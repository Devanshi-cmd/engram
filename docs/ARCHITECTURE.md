# Architecture

This document grows with each milestone. It describes what exists today, not the plan (that's [PLAN.md](PLAN.md)).

## Code layout

```
lectern/
  config.py       Settings from the environment and .env. Placeholder values count as unset.
  cli.py          `lectern serve`, `lectern version`
  web/
    app.py        FastAPI app factory: routes, security headers, themed error pages
    templates/    Jinja pages. base.html holds the header, the Help popover and the theme switch
    static/
      theme.css   DevSwarm's stylesheet, copied unchanged: tokens, header, cards, buttons, dark mode
      lectern.css Lectern-only components, built from theme.css tokens only
      site.js     Theme switch (remembered in localStorage), Help popover, upload drop zone
      htmx.min.js Partial page refreshes for live job progress
```

## Web app

- **Server-rendered.** FastAPI renders Jinja templates. Live parts (job progress) will refresh with HTMX, so there's no frontend build step.
- **Security headers on every response**, including static files and errors. A strict Content-Security-Policy (`script-src 'self'`, no inline script or style) means text read off a slide can never run as code, even if escaping were missed somewhere.
- **Host header allowlist** (`TrustedHostMiddleware`) blocks DNS-rebinding attacks on the local server.
- **Uploads are off until the pipeline exists.** `create_app(accept_uploads=...)` and at least one access code must both be set before the upload form appears.

## Theme

The look is DevSwarm's: Apple's palette (`#f5f5f7` page, `#1d1d1f` ink, `#0071e3` action), the system font stack, a frosted sticky header, 22px card radius, pill buttons, and a sun/moon switch for night mode. Light is the default. `theme.css` is never edited, so the two products stay identical. Anything new goes in `lectern.css` and uses only `var(--…)` tokens.
