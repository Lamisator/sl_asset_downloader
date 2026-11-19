# sl-asset-downloader

A command line tool that logs in to a Second Life account and downloads assets
from its inventory. You can pick the folders and asset types. The default
downloads every image in your **Photo Album** and **Textures** folders as PNG
files.

Supported asset types:

| `--type`    | saved as                                | fetched via        |
|-------------|-----------------------------------------|--------------------|
| `texture`   | `.png` (or `.jpg`, `.tga`, raw `.j2c`)  | HTTP (ViewerAsset) |
| `sound`     | `.ogg`                                  | HTTP               |
| `animation` | `.anim`                                 | HTTP               |
| `notecard`  | `.txt` (the notecard text)              | UDP transfer       |
| `script`    | `.lsl`                                  | UDP transfer       |
| `gesture`, `landmark`, `clothing`, `bodypart`, `settings`, `material` | raw asset | HTTP |

Photos (snapshots) are textures, so `--type texture` covers them too.

## Install

You need Python 3.9 or newer.

```sh
git clone https://github.com/Lamisator/sl_asset_downloader.git
cd sl_asset_downloader
pip install .
```

This installs the `sl-asset-downloader` command. To run it without installing,
use `python -m sl_asset_downloader` from the repository folder. In that case,
run `pip install Pillow` first, because Pillow converts JPEG2000 textures to
PNG.

## Usage

Download all photos and textures (the default):

```sh
sl-asset-downloader download -u "your.name"
```

The tool asks for your password. If your account uses two-factor
authentication (MFA), it also asks for the code. Like the official viewer,
it then remembers the MFA approval (in
`~/.config/sl-asset-downloader/mfa.json`), so the code isn't needed on every
run. Files go to `./sl-downloads/`, laid out like your inventory folders:

```
sl-downloads/
  Photo Album/
    Snapshot at Bay City.png
  Textures/
    Skins/
      freckles.png
```

### More examples

```sh
# List the inventory folders to find names for --folder
sl-asset-downloader folders -u "your.name"

# Only the Photo Album, as JPEG, into a specific folder
sl-asset-downloader download -u "your.name" -f @photos --format jpg -o ~/sl-photos

# Photos and textures, plus the notecards in your Notecards folder
sl-asset-downloader download -u "your.name" --notecards

# Notecards from another folder instead of the Notecards folder
sl-asset-downloader download -u "your.name" --notecards --notecard-folder "Objects/My Build"

# Notecards and sounds from specific folders
sl-asset-downloader download -u "your.name" -f @notecards -f "Objects/My Build" -t notecard -t sound

# Every supported type in the whole inventory (Trash is skipped)
sl-asset-downloader download -u "your.name" -f @root -t all

# See what would be downloaded, without downloading anything
sl-asset-downloader download -u "your.name" --dry-run
```

`--folder` accepts the following:
- a path from the top of your inventory, such as `"Textures/Skins"`
- a folder name that appears only once in your inventory
- a folder UUID
- a shortcut for a system folder: `@photos`, `@textures`, `@sounds`,
  `@notecards`, `@scripts`, `@animations`, or `@root`

`--notecards` downloads notecards in addition to whatever `--folder` and
`--type` select. By default it only takes notecards from the **Notecards**
folder (and its subfolders). Pass `--notecard-folder` (repeatable, same values
as `--folder`) to take them from other folders instead.

Subfolders are included unless you pass `--no-recursive`. Inventory links are
followed by default; `--no-links` turns that off.

When you run the tool again, it skips files it has already downloaded, so an
interrupted run continues where it stopped. It tracks downloads in
`.sl-asset-downloader.json` in the output folder. Use `--overwrite` to
download everything again.

To skip the prompts, for example in scripts, set `SL_USERNAME` and
`SL_PASSWORD` in the environment.

Other options: `--grid aditi` logs in to the beta grid. You can also pass any
login URI, such as an OpenSimulator grid. `--workers N` sets the number of
parallel downloads, and `-v`/`-vv` adds more logging. Run
`sl-asset-downloader download --help` for the full list.

## What gets downloaded (permissions)

The tool downloads an item only if one of these is true:
- you created it, or
- you have full permissions on it (copy, modify and transfer).

This is the same rule the official viewer uses for "Save As" on textures. It
follows the Second Life
[Third Party Viewer Policy](https://secondlife.com/corporate/third-party-viewers),
which does not allow exporting other people's content. Items that don't meet
the rule are counted and reported, but not downloaded.

## How it works

The tool connects to the grid the way a minimal viewer does:

1. **Login.** It sends an XML-RPC `login_to_simulator` request (with MFA
   support). The response includes the inventory folder tree.
2. **Region connection.** It opens a minimal UDP circuit with the login
   region: `UseCircuitCode`, then `CompleteAgentMovement`. It also answers
   ping checks, acknowledges reliable packets, and sends `LogoutRequest` at
   the end. This keeps the session valid while downloading. Your avatar
   appears in-world at your last location while the tool runs.
3. **Capabilities.** It requests these region capabilities:
   `FetchInventoryDescendents2`, which lists folder contents, and
   `ViewerAsset`, which downloads textures, sounds and other assets over
   HTTP.
4. **Notecards and scripts.** These are not served over HTTP. The tool
   fetches them over the UDP circuit with `TransferRequest` (source type
   "inventory item"), the same way the viewer does.

## Development

```sh
pip install -e ".[test]"
pytest
```
