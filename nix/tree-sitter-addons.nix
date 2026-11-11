# Linux builds of the two tree-sitter addons the Windows payload ships as PE
# DLLs. The local exec daemon requires both at load time, so they cannot be
# dropped like the other Windows-only addons.
#
# Cursor ships a fork named @cursor/node-tree-sitter, but only its JavaScript
# differs from upstream; the native binding is upstream tree-sitter 0.21.1.
# The app keeps its own index.js; only the .node file comes from here.
#
# There are no linux-arm64 prebuilds on npm, so both are built from source.
{
  lib,
  stdenv,
  fetchurl,
  nodejs-slim,
  node-gyp,
  python3,
}:

let
  nodeAddonApi = {
    "7" = fetchurl {
      url = "https://registry.npmjs.org/node-addon-api/-/node-addon-api-7.1.1.tgz";
      hash = "sha512-5m3bsyrjFWE1xf7nz7YXdN4udnVtXK6/Yfgn5qnahL6bCkf2yKt4k3nuTKAtT4r3IG8JNR2ncsIMdZuAzJjHQQ==";
    };
    "8" = fetchurl {
      url = "https://registry.npmjs.org/node-addon-api/-/node-addon-api-8.5.0.tgz";
      hash = "sha512-/bRZty2mXUIFY/xU5HLvveNHlswNJej+RnxBjOMkidWfwZzgTbPG1E3K5TOxRLOR+5hX7bSofy8yf1hZevMS8A==";
    };
  };

  buildAddon =
    {
      pname,
      version,
      hash,
      target,
      addonApi,
      postPatch ? "",
    }:
    stdenv.mkDerivation {
      inherit pname version;

      src = fetchurl {
        url = "https://registry.npmjs.org/${pname}/-/${pname}-${version}.tgz";
        inherit hash;
      };

      nativeBuildInputs = [
        nodejs-slim
        node-gyp
        python3
      ];

      # binding.gyp finds node-addon-api with require(), so it must sit in
      # node_modules next to the sources.
      postUnpack = ''
        mkdir -p package/node_modules/node-addon-api
        tar -xzf ${nodeAddonApi.${addonApi}} --strip-components=1 \
          -C package/node_modules/node-addon-api
      '';
      sourceRoot = "package";
      inherit postPatch;

      # Electron 42 implements N-API 10; pin 8 so the binding never uses a
      # newer API than the runtime provides, whatever nodejs builds it.
      buildPhase = ''
        runHook preBuild
        export HOME=$TMPDIR
        node-gyp configure --nodedir=${nodejs-slim} --napi_build_version=8
        node-gyp build --nodedir=${nodejs-slim} --jobs=$NIX_BUILD_CORES
        runHook postBuild
      '';

      installPhase = ''
        runHook preInstall
        install -Dm755 build/Release/${target}.node $out/${target}.node
        runHook postInstall
      '';

      meta = {
        description = "Linux build of the ${pname} native addon for Grok Bot";
        license = lib.licenses.mit;
        platforms = lib.platforms.linux;
      };
    };
in
{
  tree-sitter = buildAddon {
    pname = "tree-sitter";
    version = "0.21.1";
    hash = "sha512-7dxoA6kYvtgWw80265MyqJlkRl4yawIjO7S5MigytjELkX43fV2WsAXzsNfO7sBpPPCF5Gp0+XzHk0DwLCq3xQ==";
    target = "tree_sitter_runtime_binding";
    addonApi = "8";
    # The headers still include <node_object_wrap.h> but use only
    # Napi::ObjectWrap. That header pulls in V8, which does not compile with
    # recent Node headers, and the binding uses nothing from it except the
    # <cassert> it happened to bring along.
    postPatch = ''
      sed -i '/#include <node_object_wrap.h>/d' src/*.h
      sed -i '1i #include <cassert>' src/tree.cc
    '';
  };

  tree-sitter-bash = buildAddon {
    pname = "tree-sitter-bash";
    version = "0.21.0";
    hash = "sha512-UuXf+wliu1mmS/O2Iz7OQghExM4a+lk+GaVPndZVpAJnFuzanaN33UcHOsrmngHxaOXHz5JSZrwp6i2qM/PKag==";
    target = "tree_sitter_bash_binding";
    addonApi = "7";
  };
}
