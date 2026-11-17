# Kaitoi for DaVinci Resolve Studio

> **AI changed creation. Kaitoi changes production.**

This plugin puts your Kaitoi workflows and Kaitoi's nodes inside DaVinci Resolve. Pick a clip, pick a workflow or a node, press **Generate**. What the clip plays, with its trims, grade and effects, is sent to Kaitoi, and the result comes back onto a new track above the original. Your cut is never changed.

A workflow or node can also live in Resolve's Effects Library as an effect of its own, with its inputs as real controls in the Inspector. You then run it from the Inspector, like any other effect.

---

## What you can do

- **Run your own workflows.** Every template in your Kaitoi account that has an API endpoint is listed under **Workflows**, with its inputs. Build or change one in Kaitoi Studio and it shows up here; the plugin does not need updating.
- **Run single nodes.** **Nodes** lists Kaitoi's catalog: built-in nodes, community nodes and your own. Video models, image models, upscalers, audio tools and the rest.
- **Run from the Inspector.** **Use this workflow** or **Use this node** adds the tool to the selected clip as an effect. Its inputs appear in the Inspector under Effects, with **Generate**, **Cancel** and a status line.
- **Or run from the Kaitoi window.** **Run from this window** opens a form for the same inputs, with progress, without adding anything to the clip.
- **Keep the cut intact.** A result goes on a `Kaitoi` track at the same place as its source, and into a `Kaitoi` bin in the media pool. Switch the track off to compare. You can also send results to the end of the timeline, or to the media pool only.
- **Find things again.** Search, filter by category or task, mark favorites, and reopen any earlier result from **History**.

---

## How it fits into Resolve

A few ideas explain most of what the plugin does.

**What gets sent.** The plugin looks at the timeline in this order: the In/Out range if both marks are set, then the selected clip, then the clip under the playhead. A tool that takes a video gets that range as a small video. A tool that takes an image gets the frame under the playhead. A tool that takes audio gets the sound of the range. So one video clip can feed all three kinds of tool. With nothing selected, a tool that creates from nothing can still place its result at the playhead.

**Effect, generator or window.** Resolve only draws controls in the Inspector for effects it knows, so the plugin writes one small settings file per tool into your own Effects Library, in a folder called Kaitoi:

- An **effect**, for a tool that takes a clip or a frame. It sits on your clip, which is not moved or cut.
- A **generator**, for every tool. Laid on a track above a shot, it runs on what plays beneath it, or creates something new in its place.
- The **window** runs the same tool with the same inputs, and adds nothing to the timeline until the result arrives.

Resolve reads its effects when it starts. A tool you have just installed can be dragged in as a generator at once, but it can only be added to clips as an effect after you restart Resolve once. The window tells you when tools are waiting for a restart. Your workflows are installed for you each time the window loads them.

**Not every node suits Resolve.** The Nodes list can show everything in Kaitoi, but Resolve can only place pictures, video and sound. With **Works in Resolve** ticked, the list leaves out nodes whose result is something else, nodes that need you to work in a web page of their own, session nodes, agent nodes, and nodes that require an input the plugin cannot supply, such as a 3D model.

**More than one input.** The timeline supplies the main clip, frame or sound. Any other file a tool needs, such as a style image, has a file picker in the form and in the Inspector.

**Results that differ from the source.** Models return what they return: often a fixed length, often 720p, often 24 or 30 frames a second. The result is still placed, and a note tells you what differs, so you know before it reaches a client.

---

## Quick start

**1. Get a Kaitoi Studio account** at [studio.kaitoi.io](https://studio.kaitoi.io).

**2. Install the plugin.**

*With the macOS installer.* Download `Kaitoi-for-DaVinci-Resolve-<version>.pkg` from the [latest release](https://github.com/kaitoi-labs/kaitoi-davinci/releases/latest) and open it. It is signed and notarized by Kaitoi Labs. It installs Kaitoi for the person who runs it: each person on a shared Mac who wants Kaitoi in Resolve runs it once. If it cannot add the files to your Resolve folder, it says why in the installer log and leaves a `README.txt` beside the files with the steps to do it by hand.

*From this repository*, on macOS, Linux or Windows (Git Bash):

```bash
git clone https://github.com/kaitoi-labs/kaitoi-davinci.git
cd kaitoi-davinci
./install.sh              # link the script into Resolve's Scripts/Edit folder
./install.sh --copy       # or copy the files instead
./install.sh --uninstall  # remove it again
```

The script goes into your own scripts folder:

| OS | Folder |
|---|---|
| macOS | `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/` |
| Linux | `~/.local/share/DaVinciResolve/Fusion/Scripts/Edit/` |
| Windows | `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Edit\` |

**3. Restart Resolve**, open a timeline, and choose **Workspace > Scripts > Edit > Kaitoi**.

**4. Sign in.** On the **Account** tab press **Sign in with Kaitoi** and sign in in your browser. The plugin gets a key of its own, named for this computer and listed with your other keys in Kaitoi Studio, where you can revoke it. It stops working by itself after 30 days. Signing in again here replaces this computer's earlier key; **Sign out** forgets the key on this computer and takes your workflows out of Resolve's Effects Library. On a server that does not offer this yet, or if you prefer, paste an API key from Kaitoi Studio instead (files and runs read/write scopes; `account_credits:read` is optional), then **Save** and **Test connection**.

**5. Run something.** Select a clip. On **Nodes**, tick **Works in Resolve**, pick a node, and press **Run from this window**, then **Generate**. The result lands on a Kaitoi track above your clip.

### Uninstall

*Installed with the macOS installer.* On the **Account** tab press **Uninstall Kaitoi**, then **Uninstall**. macOS asks for your password, and the window tells you when to restart Resolve. If the window does not open, double-click `Uninstall Kaitoi.command` in `/Library/Application Support/Kaitoi/Resolve` (in Finder, **Go > Go to Folder**). It does the same.

This removes the Kaitoi script and effects from your Resolve folder, and Kaitoi's code from the Mac. The code is shared by every account of the Mac, so Kaitoi stops working in Resolve for all of them. Tick **Also remove my settings and history** to forget those too. Your results (in `~/Movies/Kaitoi` unless you changed the work folder) and your Kaitoi sign-in are never removed, and an effect you saved yourself into one of Kaitoi's folders stays where it is.

*Installed from this repository.* Run `./install.sh --uninstall`.

### From a terminal

The same script drives a running Resolve from a shell, which is handy while developing:

```bash
python3 Kaitoi.py           # open the window
python3 Kaitoi.py --check   # print what the timeline selection is, and exit
```

---

## Example usage

**Restyle a shot from the Inspector.**
> Select the clip. On **Nodes**, pick a video-to-video model and press **Use this node**. In the Inspector, open Effects, type the prompt and press **Generate**.

The clip's range is rendered small, uploaded and run, and the result is placed on a Kaitoi track at the same frame. Switch the track off and on to compare.

**Turn a frame into a shot.**
> Park the playhead on the frame. Pick an image-to-video model, press **Run from this window**, type the prompt and press **Generate**.

The frame under the playhead seeds a new shot, placed above the original.

**Run a pipeline you built in Studio.**
> Publish the template with an API endpoint in Kaitoi Studio. Press **Reload** on **Workflows**, pick it, and press **Use this workflow**.

Its inputs appear in the Inspector as text boxes, sliders, dropdowns, checkboxes and file pickers.

**Iterate.**
> Leave the playhead where it is and press **Generate** again with another prompt.

The topmost Kaitoi track is what the viewer shows, so it is what gets picked up next. Each pass builds on the last.

---

## The window

A narrow window made to sit beside the Inspector. Resolve does not let a script dock a window, so it floats, stays on top, and remembers where you left it. Four tabs: **Workflows**, **Nodes**, **History**, **Account**.

**Workflows** and **Nodes** share one layout: a search box, a **Show** filter (by task for workflows, by category for nodes), the list, and a card for the tool you picked. A line by the list says how many tools the filters leave.

- **Only what fits the timeline selection** lists only tools the current selection can feed.
- **Works in Resolve** lists only tools whose results Resolve can place.
- **Favorites only** lists the tools you marked with **Favorite** on their card.
- **Installed only**, and on Nodes the **Built-in**, **Community** and **User** switches, narrow the list further.
- On the card: **Use this workflow** or **Use this node**, **Run from this window**, and **Install in Resolve** or **Remove from Resolve**. On Nodes, **Install all** installs every listed node that is not installed yet.

**Running from the window.** The form shows what will be sent from the timeline, with **Refresh**, then the tool's inputs, **Generate** and **Cancel**. A bar and a strip show progress through the six steps:

```
Export   ›   Upload   ›   Prepare   ›   Run   ›   Download   ›   Place
```

**Cancel** stops the export, the waiting or the run on Kaitoi. **Open result** opens the finished file.

**History** has one row per finished run. **Open file** opens the result, **Place again** puts it back on the timeline, **Clear** empties the list and keeps the files.

**Account** holds the sign-in and your credit balance, then:

- *What gets sent*: **Max height** and **Quality** of the video that is uploaded, and **Send clips with their sound**.
- *Results*: **Place on** (a Kaitoi track at the source, the end of the timeline, or the media pool only), **Work folder**, **Give up after (s)**, and **Put a result clip's sound on its own audio track**.
- *In Resolve*: how many tools are in your Effects Library, **Remove everything Kaitoi installed**, and, when Kaitoi came from the macOS installer, **Uninstall Kaitoi**.
- *Window*: **Follow what is selected on the timeline** and **Stay on top**.

---

## How a run goes

```
timeline ──1. export────▶ a small video, a still or a sound file in the work folder
         ──2. upload────▶ straight to Kaitoi's storage
         ──3. prepare───▶ the workflow's endpoint, or a small graph around one node
         ──4. run───────▶ started on Kaitoi and followed until it ends
         ──5. download──▶ every file the run returned
         ──6. place─────▶ media pool "Kaitoi" bin, then a "Kaitoi N" track
```

1. **Export.** A Deliver-page render of exactly the range, scaled down to **Max height**, H.264 in MP4; a still for a tool that takes an image; the sound alone for a tool that takes audio. Every project setting the export touches is put back afterwards.
2. **Upload.** The file goes directly to storage, in parts when it is large.
3. **Prepare.** A workflow is called through its endpoint. A single node has no endpoint, so the plugin builds the smallest graph that runs it: the node, with one loader in front of each file. Pin names and types are read from the node's live schema; nothing about a node is hard-coded.
4. **Run.** The run is followed through Kaitoi's public API, which is also where the progress comes from.
5. **Download** and **6. Place.** Each file the run returned is filed in the Kaitoi bin and laid on the timeline. Nothing that was already there is touched.

Only one Kaitoi run happens at a time, across the window and the Inspector. Resolve is only ever called from the script's main thread; the network steps run on a worker thread.

---

## Configuration

| Item | Value |
|---|---|
| Server | `https://api.studio.kaitoi.io`, the origin only; the plugin adds `/api/v1` ([docs](https://api.studio.kaitoi.io/api/v1/docs)) |
| Auth | A key from **Sign in with Kaitoi**, or an API key created in Kaitoi Studio |
| Key store | `~/.kaitoi/credentials.json` (mode 0600): `api_key`, `base_url`, `web_url`; shared by Kaitoi's plugins |
| Overrides | `KAITOI_API_KEY` or `KAITOI_API` in the environment; `KAITOI_HOME` moves `~/.kaitoi` |
| Resolve scripting | Found automatically; `RESOLVE_SCRIPT_LIB` points at the `fusionscript` library of a Resolve installed somewhere unusual |
| Plugin settings | `~/.kaitoi_resolve/config.json` |
| History and log | `~/.kaitoi_resolve/history.json`, `~/.kaitoi_resolve/plugin.log` (also shown in Resolve's Console) |
| Installed tools | Settings files in the `Kaitoi` folders of your own Fusion `Templates/Edit/Effects` and `Templates/Edit/Generators`, with a record in `~/.kaitoi_resolve/templates.json`. No node code is stored there |
| Work folder | `~/Movies/Kaitoi/`: what was sent and what came back |

The key is never written into the Resolve project or the timeline.

---

## Requirements

- **DaVinci Resolve Studio.** Developed and tested on 21.2 on macOS. Windows and Linux use the same code but are untested. The free edition has no external scripting and gives scripts no UIManager, so the plugin cannot run in it.
- **Python 3** as Resolve finds it. Standard library only: there is nothing to install with pip.
- A [Kaitoi Studio](https://studio.kaitoi.io) account. Runs that use paid providers cost Kaitoi credits; the **Account** tab shows your balance.

---

## Architecture

```
Kaitoi.py                  # what Resolve lists under Workspace > Scripts > Edit; also a command line
install.sh                 # link or copy into Resolve's Scripts/Edit
kaitoi_resolve/
├─ api.py                  # Kaitoi's public API over urllib: files, workflows, runs, nodes, credits
├─ credentials.py          # ~/.kaitoi/credentials.json, the shared key store
├─ signin.py               # signing in through the browser
├─ config.py               # ~/.kaitoi_resolve/config.json and the log
├─ history.py              # ~/.kaitoi_resolve/history.json
├─ tools.py                # workflows and nodes as tools: inputs, outputs, filters
├─ graphs.py               # the small graph built around a single node
├─ templates.py            # each tool as a Resolve effect and generator
├─ inspector.py            # running a tool from the Inspector
├─ resolve_bridge.py       # everything that talks to Resolve
├─ pipeline.py             # one run: export, upload, run, download, place
├─ checks.py               # how a result differs from its source
├─ busy.py                 # one run at a time, across scripts
└─ ui.py                   # the window, built with Fusion's UIManager
packaging/                 # the macOS package and the Windows setup
tests/                     # run without Resolve: python3 -m unittest discover -s tests -t .
```

`resolve_bridge` knows nothing about Kaitoi, and `api` knows nothing about Resolve. `pipeline` is where the two meet.

### Resolve scripting notes

Learned on Resolve 21 and handled in [`resolve_bridge.py`](kaitoi_resolve/resolve_bridge.py), [`templates.py`](kaitoi_resolve/templates.py) and [`ui.py`](kaitoi_resolve/ui.py):

- A script can only open a floating window. Controls in the Inspector have to come from a Fusion template, which is why each tool gets one.
- Resolve loads effect templates when it starts. A new effect cannot be added to a clip from a script until the next start; a new generator can be dragged in at once.
- Two scripts touching the timeline at the same moment can crash Resolve, so a run announces itself and every other Kaitoi script keeps its hands off until it ends.
- `SetCurrentRenderFormatAndCodec` is refused until the Deliver state has been set up once in a session; the bridge warms it up and tries again.
- `VideoQuality` must be a whole number (0 for automatic, otherwise kb/s) for H.264 in MP4; the documented words fail the whole `SetRenderSettings` call.
- `AddRenderJob` only works reliably from the Deliver page; the bridge switches pages around the export and back.
- `AppendToTimeline`'s `endFrame` is exclusive, unlike `TimelineItem.GetEnd()`.
- Menu scripts are run without `__file__`; the script finds its package through the Scripts/Edit folder.
- A `ui.Timer` never fires inside `UIDispatcher.RunLoop()` for Python; the window uses `StepLoop()` in a loop of its own.
- A Resolve call from a worker thread blocks, and inside Resolve it deadlocks. Every Resolve call stays on the main thread.
- Inside Resolve's own interpreter, `print` from a worker thread can raise. The log is written to its file first, so nothing is lost to that.

---

## Troubleshooting

- **A tool is installed but cannot be added to a clip.** Resolve loads new effects when it starts. Restart it once; until then, drag the tool from Effects > Generators > Kaitoi onto a track above the shot, or use **Run from this window**.
- **"This Kaitoi server does not sign plugins in yet."** Paste an API key from Kaitoi Studio on the **Account** tab instead.
- **"Kaitoi is busy with ..."** Another Kaitoi run is going, in the window or in the Inspector. Wait for it or cancel it.
- **A node is missing from the list.** Untick **Works in Resolve** and **Only what fits the timeline selection**, and check the **Built-in**, **Community** and **User** switches. Your own nodes start switched off.
- **`Could not load the DaVinci Resolve scripting module`.** Only from a terminal: Resolve is not running, scripting is off in *Preferences > System > General*, or Resolve is installed somewhere unusual. For the last case, point `RESOLVE_SCRIPT_LIB` at its `fusionscript` library.
- **The window does not appear when started from a terminal.** An earlier script still holds Resolve's interface; close its window or restart Resolve.
- **Every scripting call returns `None`.** Resolve is behind a dialog, for example *Cache Location Update Required* when the cache folder is on a volume that is offline. Dismiss it.
- **Every request fails after changing the server.** The setting is the origin (`https://api.studio.kaitoi.io`), without `/api/v1`.

## Limitations

- The window floats; Resolve does not let a script dock it. Only the Inspector controls are part of Resolve's own layout.
- A script cannot open the Inspector or switch it to Effects, so after **Use this node** you open it yourself.
- A result keeps the length, size and frame rate the model gave it. It is placed at the source's position and not conformed to its length.
- One run at a time.
- The plugin shows your credit balance but does not say what a run will cost before it starts.
- Developed on macOS. The Windows setup and Linux are untested.

---

## Links

- **Product:** [studio.kaitoi.io](https://studio.kaitoi.io)
- **Company:** [kaitoi.io/labs](https://kaitoi.io/labs)
- **API docs:** [api.studio.kaitoi.io/api/v1/docs](https://api.studio.kaitoi.io/api/v1/docs)
- **MCP server:** [github.com/kaitoi-labs/kaitoi-mcp](https://github.com/kaitoi-labs/kaitoi-mcp)
- **Discord:** [discord.gg/3A5YfXnCH](https://discord.gg/3A5YfXnCH)

## About Kaitoi Labs

Kaitoi Labs, Inc. is a San Francisco-based team building hybrid intelligence tools for creative production, with a background spanning AI, filmmaking, VFX, and product design.

## License

The contents of this repository are released under the [Apache License, Version 2.0](./LICENSE). Access to Kaitoi Studio and its API is governed by the Kaitoi Studio terms of service. DaVinci Resolve is a trademark of Blackmagic Design.
