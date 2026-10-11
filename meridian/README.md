# Existing Meridian Windows integration
This folder records the existing custom Portfolio screen and its minimal October 11, 2026 correction. It is an opt-in local integration, not a new GreekSoup service or shipped starter portfolio.

The Portfolio page reads only saved Meridian holdings, intelligence and valuation history. It requires matching snapshot identity/time/quantity before displaying a saved stock value, shows quote dates, preserves adjusted options and retirement supplements, and labels the comparison chart as unadjusted value change. No private holdings or licensed provider rows are included.

The adjacent scripts/open_meridian_dashboard.vbs calls the existing free-only scripts/refresh_prices_manual.py and scripts/risk_refresh.py before opening the local Overview. Existing Research Desk task, installed Overview/Operations plugins and private saved files are prerequisites. It does not configure a broker or scheduler.

Deploy by preserving the current three files first, placing the VBS and price helper in the existing private Meridian data/scripts directory and the Portfolio HTML in the existing data/research/plugins/meridian-portfolio/screen.html. Keep risk_refresh.py beside the helper; its content was retained unchanged. This is the actual established local layout, not a general installer.

Windows syntax/dependency validation: cscript open_meridian_dashboard.vbs --check-only. Offline checks: python -m unittest discover -s tests -p test_meridian_recovery.py -v.

The launch waits for the bounded individual free-provider requests to finish. It does not guarantee fresh quotes for every security or enforce a single overall wall-clock timeout. The existing mutex prevents overlapping price writers; unknown adjusted contracts are carried with explicit failure status. Existing older scheduled exporters still require separate consolidation.

Local deployment backed up all three edited files and verified a warm launch, a new history entry, unchanged source holdings fields and visible Portfolio/Risk views. Cold-start and five-day acceptance remain open. Do not upload private data, overwrite the installed vault, enable paid providers or create an order path.
