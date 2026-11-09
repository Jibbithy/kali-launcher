# Kali Launcher

[![CI](https://github.com/Jibbithy/kali-launcher/actions/workflows/ci.yml/badge.svg)](https://github.com/Jibbithy/kali-launcher/actions/workflows/ci.yml)

A Linux desktop app for browsing and running Kali Linux tools. It finds the tools installed on your machine or inside Kali Docker containers, then opens each one in its own terminal tab.

**Only test systems you own or have written permission to test.**

## Features

- **One searchable list:** every Kali tool, from the host or any Docker container, with descriptions and each tool's own usage text.
- **Terminal tabs:** each tool runs in an embedded terminal, so several can run side by side.
- **Favorites and recents:** keep the tools you use most at the top.
- **Scope guard:** set a target (IP, CIDR range or domain) and a list of authorized targets. Write `{target}` in a command to fill it in. If a command would touch a host outside your scope, the app warns you and asks before running it.
- **Session logs:** every session is recorded to `~/kali-data/logs/` so you have a record of what ran and when.
- **GUI tools in containers:** a one-click setup lets graphical tools inside a container open windows on your desktop.

## Requirements

- Linux with GTK 3 and VTE
- Python 3
- Docker, optional, for running tools inside Kali containers

## Install and run

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
python3 kali_launcher.py
```

To use a Kali container, start one first, for example:

```bash
docker run -dit --name kali kalilinux/kali-rolling
docker exec kali bash -c "apt update && apt install -y kali-linux-default"
```

The app picks up any container with "kali" in its name or image, running or stopped, and starts it when needed.

## Where it keeps data

| Path | What |
|---|---|
| `~/.config/kali-launcher/state.json` | Favorites, recents, target and scope list |
| `~/kali-data/logs/` | Session logs, one file per tool run |
