# Grok Bot ships for Windows and macOS only. This takes the official Windows
# installer, keeps the platform-neutral Electron app inside it, and runs it on
# the matching Linux Electron from nixpkgs. Windows-only native addons are
# removed; the app loads each of them behind a try/catch and turns the
# matching feature off. The two tree-sitter addons it cannot run without are
# rebuilt from source.
{
  lib,
  stdenvNoCC,
  fetchurl,
  callPackage,
  _7zz,
  asar,
  makeWrapper,
  wrapGAppsHook3,
  copyDesktopItems,
  makeDesktopItem,
  gtk3,
  gsettings-desktop-schemas,
  xdg-utils,
  electron,
  sources ? lib.importJSON ../sources.json,
  # The app is built and tested by upstream against one Electron major. The
  # build fails on a mismatch unless this is set.
  allowElectronMismatch ? false,
}:

let
  addons = callPackage ./tree-sitter-addons.nix { };
  electronDist = "${electron}/libexec/electron";
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "grok-bot";
  inherit (sources) version;

  src = fetchurl {
    inherit (sources) url hash;
  };

  nativeBuildInputs = [
    _7zz
    asar
    makeWrapper
    wrapGAppsHook3
    copyDesktopItems
  ];

  buildInputs = [
    gtk3
    gsettings-desktop-schemas
  ];

  unpackPhase = ''
    runHook preUnpack
    7zz x -y -onsis "$src" '$PLUGINSDIR/app-64.7z' > /dev/null
    7zz x -y -owin 'nsis/$PLUGINSDIR/app-64.7z' > /dev/null
    runHook postUnpack
  '';

  buildPhase = ''
    runHook preBuild

    upstream=$(grep -aoE -m1 'Electron/[0-9]+\.[0-9]+\.[0-9]+' 'win/Grok Bot.exe' | cut -d/ -f2)
    upstream=''${upstream%%$'\n'*}
    echo "upstream Electron $upstream, runtime Electron ${electron.version}"
    if [ "''${upstream%%.*}" != "${lib.versions.major electron.version}" ] \
      && [ -z "${lib.optionalString allowElectronMismatch "1"}" ]; then
      echo "Grok Bot ${finalAttrs.version} is built for Electron $upstream." >&2
      echo "Set \"electron\" in sources.json to ''${upstream%%.*}, or pass allowElectronMismatch = true." >&2
      exit 1
    fi

    # Unpack the whole archive, including the files kept outside it, into a
    # plain directory. Electron loads resources/app the same way it loads
    # resources/app.asar, and new files need no asar header entries.
    asar extract win/resources/app.asar app

    find app -type f \( -name '*.node' -o -name '*.exe' -o -name '*.dll' \) -print -delete

    deps=app/dist/deps
    install -Dm755 ${addons.tree-sitter}/tree_sitter_runtime_binding.node \
      $deps/tree-sitter/build/Release/tree_sitter_runtime_binding.node
    rm -rf $deps/tree-sitter-bash/prebuilds
    install -Dm755 ${addons.tree-sitter-bash}/tree_sitter_bash_binding.node \
      $deps/tree-sitter-bash/build/Release/tree_sitter_bash_binding.node

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    lib=$out/lib/grok-bot
    mkdir -p $lib/resources

    # app.isPackaged is true only when the executable is not named
    # "electron", and Chromium resolves its data files next to
    # /proc/self/exe. So the binary is copied under a new name (a symlink
    # would resolve back to electron) and the rest is linked beside it.
    cp ${electronDist}/electron $lib/grok-bot
    for f in ${electronDist}/*; do
      case "$(basename "$f")" in
        electron|resources) ;;
        *) ln -s "$f" $lib/ ;;
      esac
    done

    cp -r app $lib/resources/app
    # The app builds some paths as resources/app.asar.unpacked/dist/...
    ln -s app $lib/resources/app.asar.unpacked

    install -Dm644 app/dist/renderer/assets/app-icon-*.png \
      $out/share/icons/hicolor/256x256/apps/grok-bot.png

    runHook postInstall
  '';

  desktopItems = [
    (makeDesktopItem {
      name = "grok-bot";
      desktopName = "Grok Bot";
      comment = "Grok Bot desktop agent (unofficial Linux build)";
      exec = "grok-bot %U";
      icon = "grok-bot";
      startupWMClass = "grok-bot";
      categories = [
        "Network"
        "Office"
      ];
      mimeTypes = [
        "x-scheme-handler/grokbot"
        "x-scheme-handler/sand"
      ];
      keywords = [
        "Grok"
        "Cursor"
        "xAI"
      ];
    })
  ];

  # The Electron binary is prebuilt and already patched by nixpkgs, and the
  # app's scripts must stay byte-for-byte as shipped.
  dontStrip = true;
  dontPatchELF = true;
  dontPatchShebangs = true;
  dontWrapGApps = true;

  # shell.openExternal (used for sign-in) needs xdg-open on PATH. The wrapper
  # must be a shell script: wrapGAppsHook3 makes makeWrapper build a binary
  # wrapper, which would pass the ''${NIXOS_OZONE_WL...} flag text through
  # unexpanded.
  postFixup = ''
    makeShellWrapper $out/lib/grok-bot/grok-bot $out/bin/grok-bot \
      "''${gappsWrapperArgs[@]}" \
      --prefix PATH : ${lib.makeBinPath [ xdg-utils ]} \
      --add-flags "\''${NIXOS_OZONE_WL:+\''${WAYLAND_DISPLAY:+--ozone-platform-hint=auto --enable-features=WaylandWindowDecorations --enable-wayland-ime=true}}"
  '';

  passthru = {
    inherit addons electron;
  };

  meta = {
    description = "Grok Bot desktop app, repackaged from the official Windows build for Linux";
    homepage = "https://cursor.com";
    license = lib.licenses.unfree;
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
    mainProgram = "grok-bot";
  };
})
