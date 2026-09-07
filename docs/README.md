# docs/

`index.html` is the **Voice Flows** site — one page per action, showing what the
integration says back when you drive it by voice.

It is a single self-contained file. No build step, no dependencies, no JavaScript loaded
from anywhere; the only external request is the Google Fonts stylesheet, and there is a
real fallback stack behind every face. Each action has its own address — `#schedule`,
`#cancel`, `#skip`, `#list`, `#find`, `#edit` — so browser back works and a single flow
can be linked to directly.

## Publishing it

Settings → Pages → **Deploy from a branch** → branch `main`, folder **`/docs`**.

It then serves at `https://<user>.github.io/<repo>/`, and the link in the README needs
pointing there.

## Editing it

Everything is in the `PAGES` array near the foot of the file: one object per action, with

- `title` / `action` — the page name, and the bare verb used in the flow heading
- `flows` — the numbered cause → response rows. `out` for a single reply, or `ask` plus
  `fork` for a question with two outcomes
- `says` — the tables underneath
- `extra` — any callouts

Colors are `ok` (successful), `ask` (a question back), `warn` (successful, default time
used) and `no` (unsuccessful). **Searched and found nothing is `ok`** — an empty answer is
still an answer; only a request that could not be carried out at all is `no`.

Every quoted line comes from `custom_components/fiendishly_reminders/intents.py`. If you
change the wording there, change it here.
