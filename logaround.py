#!/usr/bin/env python3
"""
logaround.py - Search and highlight system logs using journalctl and pandas.

Features:
- Flexible search: filter by date/time and multiple terms (AND).
- Highlight matched terms in color using Rich.
- Outputs a pretty table; falls back to last N logs if no term provided.
- Never loses lines, even on parse miss. Context (±delta) is truly implemented.
- NEW: Supports human-friendly --since/--until by using GNU date -d for parsing.
"""

import subprocess
import pandas as pd
import re
from datetime import datetime, timedelta
from rich.console import Console
from rich.table import Table
from rich.text import Text
import argparse
import sys

console = Console()

def run_journalctl(since=None, until=None, lines=None):
    """
    Run journalctl with optional since/until and line limit. Returns plain text output.
    """
    cmd = ['journalctl', '--no-pager', '-o', 'short']
    if since:
        cmd += ['--since', since]
    if until:
        cmd += ['--until', until]
    if lines:
        cmd += ['-n', str(lines)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]journalctl error: {result.stderr.strip()}[/red]")
        sys.exit(1)
    return result.stdout

def gnu_date_parse(timestr):
    """
    Take a human-friendly date string, and convert it to ISO 8601 using GNU date.
    If parsing fails, return None.
    """
    if not timestr:
        return None
    try:
        result = subprocess.run(['date', '-d', timestr, '+%Y-%m-%d %H:%M:%S'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def parse_journalctl_output(output):
    """
    Parse journalctl output lines into a DataFrame with columns:
    timestamp, host, unit, message.
    If a line can't be parsed, put the whole thing in 'message' and leave other fields blank.
    This ensures output never drops lines due to format.
    """
    regex = r'(?P<timestamp>\w{3} +\d{1,2} +\d{2}:\d{2}:\d{2}) (?P<host>\S+) (?P<unit>[^:\[]+)(?:\[\d+\])?: (?P<message>.*)'
    rows = []
    for line in output.splitlines():
        match = re.match(regex, line)
        if match:
            data = match.groupdict()
        else:
            # Fallback: keep whole line, nothing gets dropped!
            data = {
                'timestamp': '',
                'host': '',
                'unit': '',
                'message': line
            }
        rows.append({k: v.strip() for k, v in data.items()})
    return pd.DataFrame(rows)

def highlight_message(msg, terms):
    """
    Return a Rich Text object with all search terms highlighted, case-insensitive.
    """
    t = Text(msg)
    for term in terms:
        regex = re.compile(re.escape(term), re.IGNORECASE)
        t.highlight_regex(regex, style="bold yellow on red")
    return t

def search_logs(df, terms, delta=0):
    """
    Return DataFrame rows where all terms appear in the message column (case-insensitive).
    If delta > 0, include ±delta lines of context around each match.
    This implementation never drops any lines due to parsing.
    """
    if not terms:
        return df
    mask = pd.Series(True, index=df.index)
    for term in terms:
        mask &= df['message'].str.contains(term, case=False, na=False)
    match_idxs = df[mask].index.tolist()
    if delta == 0 or not match_idxs:
        return df[mask]
    context_idxs = set()
    for idx in match_idxs:
        context_idxs.update(range(max(0, idx - delta), min(len(df), idx + delta + 1)))
    context_idxs = sorted(context_idxs)
    return df.iloc[context_idxs]

def print_table(df, terms, max_rows=100):
    """
    Pretty-print a table of log results, with highlighted search terms.
    """
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Time", style="dim")
    table.add_column("Unit")
    table.add_column("Message", style="white")
    shown = 0
    for _, row in df.iterrows():
        table.add_row(
            row.get('timestamp', ''),
            row.get('unit', ''),
            highlight_message(row.get('message', ''), terms)
        )
        shown += 1
        if shown >= max_rows:
            break
    console.print(table)

def main():
    parser = argparse.ArgumentParser(
        prog="logaround.py",
        description=(
            "Search and highlight system logs using journalctl and pandas.\n\n"
            "Flexible, human-friendly search for systemd logs by date/time and ANDed terms.\n"
            "Matches are highlighted in color, and output is shown in a pretty terminal table.\n"
            "Supports fuzzy --since/--until (e.g. 'yesterday 15:00', '1 hour ago').\n"
            "\n"
            "Examples:\n"
            "  logaround.py --term fail\n"
            "  logaround.py --since '2 hours ago' --term error --term sshd\n"
            "  logaround.py --since yesterday --until now --term reboot --delta 2\n"
            "\n"
            "If no search terms are given, the most recent logs are shown."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False
    )
    parser.add_argument('-h', '--help', action='help', help='Show this help message and exit.')
    parser.add_argument('-v', '--version', action='version', version='logaround.py 1.1.0')
    parser.add_argument('--since', type=str, default=None, help='Start time for journalctl (e.g. "2024-07-29 00:00", "last tuesday 14:00"). Supports fuzzy times.')
    parser.add_argument('--until', type=str, default=None, help='End time for journalctl (e.g. "today 14:00", "2 days ago 17:30").')
    parser.add_argument('--term', type=str, action='append', help='Search term. Use multiple times for AND search (e.g. --term ssh --term fail).')
    parser.add_argument('--lines', type=int, default=500, help='How many lines to fetch if not filtering by time. Default: 500.')
    parser.add_argument('--max', type=int, default=100, help='Maximum number of rows to display. Default: 100.')
    parser.add_argument('--delta', type=int, default=0, help='Show ±N context lines before/after each match. Default: 0 (no context).')
    args = parser.parse_args()

    # Convert any fuzzy times to ISO 8601 for journalctl (GNU date parsing)
    since = gnu_date_parse(args.since) if args.since else None
    until = gnu_date_parse(args.until) if args.until else None

    # Fetch logs
    output = run_journalctl(since=since, until=until, lines=args.lines)
    df = parse_journalctl_output(output)

    # Filter/search, context preserved
    filtered = search_logs(df, args.term if args.term else [], delta=args.delta)

    if filtered.empty:
        console.print("[yellow]No results found for your search.[/yellow]")
    else:
        print_table(filtered, args.term if args.term else [], max_rows=args.max)

if __name__ == '__main__':
    main()
