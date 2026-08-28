# Opportunity Radar

Personal Opportunity Radar system.

## Run Milestone 1 with no API key

Deterministic mode is the v0.1 default:

```powershell
python -m scripts.evaluate_urls data/milestone1_urls.txt
```

Or select it explicitly:

```powershell
$env:OPPORTUNITY_RADAR_MODE = "deterministic"
python -m scripts.evaluate_urls data/milestone1_urls.txt
```

Put exactly ten nonblank HTTP/HTTPS URLs in `data/milestone1_urls.txt`. Optional factual corrections may be placed in `config/opportunity_overrides.yaml`. No OpenAI key is required.

To opt into OpenAI factual extraction later:

```powershell
$env:OPPORTUNITY_RADAR_MODE = "openai"
$env:OPENAI_API_KEY = "your-api-key"
$env:OPENAI_MODEL = "your-structured-output-capable-model"
python -m scripts.evaluate_urls data/milestone1_urls.txt
```

OpenAI mode currently uses OpenAI for factual extraction and the same deterministic personalized assessor used by zero-cost mode.

## Developer fetch inspection

To manually inspect HTTP fetching and deterministic HTML preparation for one real page:

```bash
python -m scripts.fetch_url "https://example.com/opportunity"
```

This command is for development inspection only.

## Developer factual extraction

Configure credentials in the process environment; do not place secrets in source files:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
$env:OPENAI_MODEL = "your-structured-output-capable-model"
```

Then fetch and factually extract one page:

```powershell
python -m scripts.extract_url "https://example.com/opportunity"
```

This command outputs factual Opportunity JSON only. It does not perform personalized assessment.

