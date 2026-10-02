# rajnish sahani — instagram telegram bot

created by rajnish sahani

send a direct instagram post, reel, carousel, or individual story link to your
telegram bot. it returns the best media the downloader/session can access as
telegram documents, which avoids telegram image compression. it does not upscale
media or promise access to instagram's internal source originals.

## deploy on render

1. create a **private** github repository named `rajnish-sahani`.
   initialize it with a readme so it has a `main` branch. upload this project's
   contents at the repository root, including the dotfiles. never upload `.env`
   or instagram cookies.
2. connect that repository to render. choose **new → blueprint** to use
   `render.yaml`, or **new → web service**, docker runtime, and the **free** plan.
3. set `BOT_TOKEN` to your botfather token in render's environment settings.
4. set `ALLOWED_USER_IDS` to your numeric telegram user id. if you do not know it,
   initially leave this blank, deploy, send `/id` to your bot, then enter the
   returned number in render and redeploy. a blank allowlist permits anyone.
5. deploy. render supplies `PORT` and `RENDER_EXTERNAL_URL`; the bot registers its
   authenticated webhook automatically. you do not need to set `WEBHOOK_URL`.
6. send `/start` and a direct instagram media link in telegram.

run only one instance of this bot token. stop any existing polling bot before
using this deployment. a render deploy is not complete until the logs show that
the bot started successfully and `/start` replies in telegram.

## limitations of the free service

render spins free web services down after 15 minutes without incoming traffic.
the next telegram request can take about a minute to wake the service; telegram
may retry webhook delivery. queued messages are no longer deliberately discarded
at startup, but updates already accepted into memory and active downloads can
still be lost on a restart. send the link again if necessary.

free instance hours, bandwidth, build limits and render's external-traffic limits
apply. this is a personal hobby deployment, not guaranteed always-on hosting.
see https://render.com/docs/free for current details.

## instagram login

public content may work without cookies. instagram can block anonymous requests
and cloud-hosted IPs. stories and login-required content commonly need a session
that already has access. cookies are optional, not a guarantee of success.

export your own instagram session in netscape cookies format. locally, encode it:

```bash
base64 -w 0 cookies.txt
```

on macos:

```bash
base64 < cookies.txt | tr -d '\n'
```

paste the result into **render → environment → `IG_COOKIES_B64`**, then redeploy.
keep tokens and cookies out of github and chat. clear/replace cookies when they
expire. the bot does not require your instagram password.

## what happens to files

media and cookies are created in a temporary request directory. normal completion,
download failures, upload failures, cancellation and timeouts clean that directory.
child downloader processes are stopped before it is removed. gallery-dl's database
cache is in memory, yt-dlp's disk cache is disabled, and incidental cache paths
are confined to the same temporary directory.

an abrupt machine/container kill cannot execute python cleanup; render's ephemeral
filesystem is discarded on restart, redeploy and spin-down. telegram receives and
stores the files you send; this cleanup refers to the bot server only.

requests are limited to 20 files and approximately 256 mib of temporary media by
default. the size monitor checks every 0.25 seconds, so this is not a hard disk quota.
files above 49 mib are skipped, without lowering quality. this build uses the
standard hosted telegram bot api; changing the upload limit alone does not enable
larger uploads. yt-dlp and ffmpeg are included to fetch and merge video/audio
without re-encoding when instagram provides separate streams.

## commands

- `/start`: introduction and creator credit
- `/help`: usage
- `/id`: your own numeric telegram user id

## local use

with docker:

```bash
cp .env.example .env
# edit .env with your token and numeric user id
# leave WEBHOOK_URL blank for local polling
docker compose up -d --build
```

without docker, install python 3.11+ and ffmpeg, then:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# set BOT_TOKEN and ALLOWED_USER_IDS in your shell environment securely
python bot.py
```

`.env` is read by docker compose, not automatically by `python bot.py`.
`TELEGRAM_BOT_TOKEN` is also accepted if `BOT_TOKEN` is absent.

## checks

```bash
python -m unittest discover -s tests -v
```

the offline tests cover url validation, allowlisting, render/polling startup,
webhook authentication, secret redaction, partial results, cleanup on upload
failures, downloader cancellation/timeouts and temporary storage limits. a real
instagram-to-telegram download still requires your deployment, token and test link.
