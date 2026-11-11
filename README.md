# Grok Bot for Linux

An unofficial Nix package that runs the **Grok Bot** desktop app on Linux,
on both **x86_64** and **aarch64**.

Grok Bot only ships for Windows and macOS. Its app is written in JavaScript
on Electron, so almost all of it runs on Linux unchanged. This flake takes
the official Windows installer, keeps the app, and runs it on the Linux
build of the same Electron version. No Wine.

This is not an xAI or Cursor product. Linux is not supported by them.

## Run it

```bash
nix run github:portal-wheatley/Grok-Bot-for-Linux
```

Sign in with the same account you use on the Mac or Windows app. The
**Sign in** button opens your browser; the app picks up the login by itself.

## Install it

### NixOS / home-manager, through the flake

```nix
# flake.nix
inputs.grok-bot.url = "github:portal-wheatley/Grok-Bot-for-Linux";

# NixOS module
environment.systemPackages = [ inputs.grok-bot.packages.${pkgs.stdenv.hostPlatform.system}.default ];
# or home-manager
home.packages = [ inputs.grok-bot.packages.${pkgs.stdenv.hostPlatform.system}.default ];
```

The flake's own package already allows the two things it needs (see
below), so this works without changing your nixpkgs config.

### Through the overlay

The overlay builds `pkgs.grok-bot` from your own nixpkgs. Your config must
then allow it:

```nix
nixpkgs.overlays = [ inputs.grok-bot.overlays.default ];
nixpkgs.config = {
  allowUnfreePredicate = pkg: lib.getName pkg == "grok-bot";
  permittedInsecurePackages = [ "electron-42.11.18" ]; # whatever version your nixpkgs has
};
environment.systemPackages = [ pkgs.grok-bot ];
```

Installing through either route adds a desktop entry and registers the
`grokbot://` and `sand://` links, so links from the web open the app.

### Why "unfree" and "insecure"

- **Unfree:** Grok Bot is proprietary. The package is marked that way.
- **Insecure:** Grok Bot 0.79.4 is built for Electron 42. nixpkgs marks
  Electron 42 as end of life. The official Windows and macOS apps ship that
  same Electron, so this is no worse than upstream. The flake allows only
  that one Electron major, nothing else.

## How it works

1. Download the official Windows installer from Cursor's CDN. Its version
   and hash are pinned in `sources.json`.
2. Unpack it with 7-Zip and unpack the app archive (`app.asar`) into a plain
   directory.
3. Delete every Windows native addon (`.node`, `.exe`, `.dll`).
4. Rebuild the two addons the app cannot run without, `tree-sitter` and
   `tree-sitter-bash`, from their npm sources. They are open source.
5. Put the app next to the matching Electron from nixpkgs (`electron_42-bin`).
   The Electron binary is copied under the name `grok-bot`, so Electron
   treats the app as an installed app, not a development run.
6. Wrap it with `xdg-open` on the PATH (for sign-in) and the usual Wayland
   flags when `NIXOS_OZONE_WL` is set.

The build stops with an error if upstream moves to a different Electron
major than the one in `sources.json`. That keeps the app on the runtime it
was built for.

## What works and what does not

The app loads each Windows-only addon inside a `try`/`catch`. When one is
missing, the app turns that feature off and carries on. On Linux that means:

| Feature | Status |
|---|---|
| Sign-in, bots, the cloud computer, chat | Works (it is the same app) |
| Bash command parsing (`tree-sitter`) | Works, rebuilt from source |
| Image previews (`jpeg-napi`) | Works, the app falls back to Electron's image code |
| Address suggestions (Apple Maps) | Off |
| Reading text from card photos | Off |
| Apple DeviceCheck | Off |
| Reading OS notification settings | Off |
| Capturing local windows for a bot | Off |
| Local passkeys and security keys on the cloud computer (`sand-webauthn-signer`) | Off |
| Process usage metrics (`cursor-proclist`) | Off on purpose: it only reports your process list to Cursor |
| In-app auto-update | Off. Update this flake instead |

## Updating

```bash
nix develop -c ./update.sh
nix build && ./result/bin/grok-bot
```

`update.sh` asks Cursor's update service for the newest release, then
writes its version, download URL, hash and Electron major to
`sources.json`. If the Electron major changed, check that nixpkgs has
`electron_<major>-bin` before committing.

## Testing status

- **x86_64:** built and run under Xvfb. The sign-in screen renders, the
  Sign in button opens the browser through `xdg-open`, a second launch with
  a `grokbot://` link hands it to the running window, and both tree-sitter
  addons load and parse inside Electron's runtime. Not yet used signed in.
- **aarch64:** the flake evaluates, and the tree-sitter addons cross-compile
  to aarch64. Apart from those two small addons, an aarch64 build only
  downloads and repackages. Not yet run on real aarch64 hardware.

## Credits

The idea of starting from the Windows installer instead of the macOS disk
image comes from
[jakob-bu/grok-bot-linux-unofficial](https://github.com/jakob-bu/grok-bot-linux-unofficial).

## License

The Nix code and scripts here are MIT licensed. Grok Bot itself is not; see
[NOTICE.md](NOTICE.md).
