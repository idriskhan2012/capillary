# Source Posts (ground truth for enhancement work)

These 16 `.txt` files are the actual scraped text of every post from
https://thecapillary.substack.com/ (fetched via Substack's public archive + posts API,
HTML stripped to plain text). They are the ground truth the `stocks` and `models` arrays
in `public/data.json` were originally summarized from.

**Do not re-scrape Substack to regenerate these** — they're already here, and re-scraping
risks pulling edited/changed post content that no longer matches what the tracker's
existing entries were written against. Only re-scrape if a post listed here has genuinely
been updated and you're deliberately refreshing it.

## Which file maps to which stock or model

`public/data.json` already contains this mapping in two JS objects — read those directly
rather than maintaining a second copy here:
- `stockPosts` — ticker → one or more `{title, slug}` entries
- `modelPost` — model name → the single post where it was introduced

A file's slug (e.g. `kpit-technologies-crash-how-the-two.txt`) corresponds to the `slug`
field in those objects. Some stocks/models draw on more than one post — check `stockPosts`
for the full list per ticker before assuming a single file has everything.
