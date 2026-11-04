# int3 — partner guide (pilot)

Thanks for trying int3. It puts your company's knowledge behind Hermes, an AI assistant app:
you ask a question in plain language and get an answer drawn only from what you are allowed to
see, with sources. This pilot uses **made-up company data**; nothing you type is used to train a
model.

## What you need

- A **Windows 11** PC, version **22H2 or later** (Settings › System › About › Windows
  specifications). Windows 10 cannot run the official Hermes app.
- The **invite link** we sent you. It works **once**, so do not forward it. If it fails, ask us
  for a new one.

## Install (about 5 minutes)

1. **Install Hermes Desktop**, the official app from Nous Research:
   [hermes-agent.nousresearch.com/desktop](https://hermes-agent.nousresearch.com/desktop), which
   opens the Microsoft Store. You do not need to set Hermes up or sign in to anything in it.
2. **Install int3.** Download `int3_<version>_x64-setup.exe` from the link we sent.
   - Windows will show **"Windows protected your PC"**. That is because this early version is not
     code-signed yet.
   - Click **More info**, check it says *int3_…_x64-setup.exe*, then click **Run anyway**.
   - It installs for your Windows account only, with no admin prompt.
3. **Open int3**, paste your invite link, and click **Sign in**. You will see your company's
   brains by name.
4. Click **Launch Hermes**. Hermes opens connected as **int3 - you@yourcompany**. Ask away.

Each launch connects Hermes for 12 hours. After that, click **Launch Hermes** in int3 again.

## Things that look odd but are expected

- **Messaging** in Hermes is empty. Messaging bots are not part of the pilot.
- **Capabilities** says "Skills failed to load". The skills catalogue is switched off; the skills
  that are installed still work.
- **Settings › About** says it couldn't reach the update server. We keep your Hermes backend on
  a tested version ourselves.

## What stays on your laptop

- **int3** keeps only the server address and a sign-in token, stored in Windows Credential
  Manager. Both are deleted when you sign out.
- **Hermes** keeps a cache of your most recent conversations, so a reopened chat shows up
  instantly. This is Hermes's own standard behaviour; it holds up to 50 recent chats. So the last
  answers you saw stay on this PC even after you sign out of int3. That is fine for this pilot,
  which uses made-up data. Tell us if you need it cleared.

## Signing out or switching person

In int3, open **Account** and choose **Sign out** or **Switch user**. Hermes disconnects from your
company brain right away, and your sign-in stops working within about 30 seconds.

## Problems

Send us a screenshot and roughly what time it happened. Never send us your invite link after
you've used it, and never send passwords or keys.
