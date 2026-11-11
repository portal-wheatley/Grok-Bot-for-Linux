{
  description = "Grok Bot (Windows/macOS only) as a native Linux app, for x86_64 and aarch64";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      sources = lib.importJSON ./sources.json;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      # Upstream pins an Electron major that nixpkgs may already mark as
      # insecure (end of life). Grok Bot itself ships that same Electron on
      # Windows and macOS, so allow exactly that major and nothing else.
      isPinnedElectron =
        pkg: lib.getName pkg == "electron" && lib.versions.major (lib.getVersion pkg) == sources.electron;

      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config = {
            allowUnfreePredicate = pkg: lib.getName pkg == "grok-bot";
            allowInsecurePredicate = isPinnedElectron;
          };
        };

      grokBotFor =
        pkgs:
        pkgs.callPackage ./nix/package.nix {
          inherit sources;
          electron = pkgs."electron_${sources.electron}-bin";
        };
    in
    {
      overlays.default = final: _prev: {
        grok-bot = grokBotFor final;
      };

      packages = lib.genAttrs systems (
        system:
        let
          grok-bot = grokBotFor (pkgsFor system);
        in
        {
          inherit grok-bot;
          default = grok-bot;
        }
      );

      apps = lib.genAttrs systems (system: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${system}.default;
        };
      });

      devShells = lib.genAttrs systems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              curl
              jq
              _7zz
            ];
          };
        }
      );

      formatter = lib.genAttrs systems (system: (pkgsFor system).nixfmt);
    };
}
