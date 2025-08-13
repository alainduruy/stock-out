# Inventory Recap Agent

Automated inventory management system that sends daily Slack reports about items selling out.

## Features

- Daily automated reports at 9 AM
- Identifies items selling out within the next 30 days
- Special tracking for "Proclub" and "Camber" brands
- Groups items by sell-out date, then alphabetically
- Shows already sold-out items (0 stock)

## Setup

### GitHub Secrets

To enable the daily automated reports via GitHub Actions, add these secrets to your repository:

1. Go to your GitHub repository
2. Navigate to Settings → Secrets and variables → Actions
3. Add the following repository secrets:

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Service Account JSON credentials | `{"type": "service_account", ...}` |
| `SPREADSHEET_ID` | Google Sheets spreadsheet ID | `1dlDGPVueBc5A1z-jPlfTlW-GGjBAAGYDRXg88aTFRFA` |
| `SHEET_NAME` | Name of the sheet tab | `REORDER_DASHBOARD` |
| `SLACK_BOT_TOKEN` | Slack bot token | `xoxb-1507390174117-...` |
| `SLACK_CHANNEL` | Slack channel for reports | `#logistique` |
| `TZ` | Timezone for date formatting | `Europe/Paris` |
| `DATE_FORMAT` | Date format string | `%Y-%m-%d` |

### Local Development

1. Copy `.env.example` to `.env` and fill in your credentials
2. Install dependencies: `pip install -r requirements.txt`
3. Run manually: `python inventory_recap_agent.py`

## Schedule

The GitHub Actions workflow runs automatically every day at 9:00 AM UTC. You can also trigger it manually from the Actions tab in your GitHub repository.

## Output Format

The report includes three sections:
1. **Items selling out in the next 7 days** - Most urgent items
2. **Items selling out in 8-30 days** - Medium priority items  
3. **All items selling out this month** - Complete overview grouped by date

Items are grouped by supplier, then by sell-out date, with alphabetical sorting within each group.