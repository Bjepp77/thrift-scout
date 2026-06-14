# Thrift Scout

Automated ShopGoodwill.com monitor. Searches for specific brands and sizes, dedupes against past results, and emails a daily HTML digest.

Runs unattended via GitHub Actions — no terminal, no manual steps.

## How it works

1. GitHub Actions cron fires daily at 7 AM MT
2. Searches ShopGoodwill's API for each brand/size in `config.yaml`
3. Filters out items already seen (stored in Supabase)
4. Sends each profile a separate email with new matches
5. Optionally adds matches to a ShopGoodwill watchlist

## Adding a brand

Edit `config.yaml` and add a target under your profile:

```yaml
- brand: "Nike"
  aliases: ["Nike"]
  sizes: ["11", "US 11"]
  gender: "mens"
  exclude: ["kids", "youth", "womens"]
```

- **aliases** — alternative spellings to match in listing titles
- **sizes** — size strings to look for (common aliases like "XL"/"Extra Large" are expanded automatically)
- **gender** — auto-excludes opposite gender terms
- **exclude** — title keywords that disqualify a match
- **match_mode** — set to `"keyword_pair"` for items without sizes (e.g., watches)
- **max_price** — optional price cap

Commit and push. The next daily run picks it up.

## Secrets (GitHub Actions)

| Secret | Purpose |
|--------|---------|
| `EMAIL_SENDER` | Gmail address that sends the digests |
| `EMAIL_PASSWORD` | Gmail app password |
| `SGW_USERNAME` | ShopGoodwill login (for watchlist only) |
| `SGW_PASSWORD` | ShopGoodwill password |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon API key |

## Manual run

Trigger from the **Actions** tab on GitHub, or:

```
gh workflow run "Daily Thrift Scout Scan"
```

## Local preview (no emails sent)

```
pip install -r requirements.txt
export SUPABASE_URL=... SUPABASE_ANON_KEY=...
python -m thrift_scout run --preview output
```

Writes HTML files to `output.Brandon.html`, `output.Alysha.html`.
