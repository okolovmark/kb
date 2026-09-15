{
  description = "kb: personal knowledge graph and todo on Neo4j";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";

    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
  };

  outputs = { self, nixpkgs, flake-utils, uv2nix, pyproject-nix, pyproject-build-systems, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        lib = nixpkgs.lib;
        python = pkgs.python314;
        neo4j = pkgs.neo4j;

        # uv.lock is the source of truth; the flake only reads it.
        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        uvLockedOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        pythonSet =
          (pkgs.callPackage pyproject-nix.build.packages { inherit python; })
          .overrideScope (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            uvLockedOverlay
          ]);

        kbEnv = pythonSet.mkVirtualEnv "kb-env" workspace.deps.default;

        # The dev venv installs kb editable from $REPO_ROOT/src, so pytest and the
        # kb/kb-setup binaries in `nix develop` run the working tree, not a store copy.
        editableOverlay = workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; };
        editablePythonSet = pythonSet.overrideScope (lib.composeManyExtensions [
          editableOverlay
          (final: prev: {
            kb = prev.kb.overrideAttrs (old: {
              # hatchling needs only the metadata and the package dir for an editable wheel
              src = lib.fileset.toSource {
                root = old.src;
                fileset = lib.fileset.unions [
                  (old.src + "/pyproject.toml")
                  (old.src + "/README.md")
                  (old.src + "/src/kb/__init__.py")
                ];
              };
              nativeBuildInputs = old.nativeBuildInputs
                ++ final.resolveBuildSystem { editables = [ ]; };
            });
          })
        ]);
        kbDevEnv = editablePythonSet.mkVirtualEnv "kb-dev-env" workspace.deps.all;

        # kb-setup writes this exact store path into the systemd unit, and kb
        # calls its neo4j-admin for backup/restore. systemctl is deliberately not
        # on the wrapper PATH: it must be the host's.
        kb = pkgs.stdenv.mkDerivation {
          pname = "kb";
          version = pythonSet.kb.version;
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          installPhase = ''
            mkdir -p $out/bin
            for bin in kb kb-setup; do
              makeWrapper ${kbEnv}/bin/$bin $out/bin/$bin \
                --set KB_NEO4J_PACKAGE ${neo4j}
            done
          '';
          meta.mainProgram = "kb";
        };
      in
      {
        packages = {
          default = kb;
          inherit kb neo4j;
        };

        apps = {
          default = { type = "app"; program = "${kb}/bin/kb"; };
          kb-setup = { type = "app"; program = "${kb}/bin/kb-setup"; };
        };

        devShells.default = pkgs.mkShell {
          packages = [ kbDevEnv pkgs.uv neo4j ];
          env = {
            KB_NEO4J_PACKAGE = "${neo4j}";
            # uv must not manage a venv beside the nix one
            UV_NO_SYNC = "1";
            UV_PYTHON = "${python}/bin/python3.14";
            UV_PYTHON_DOWNLOADS = "never";
          };
          shellHook = ''
            unset PYTHONPATH
            # the editable .pth resolves $REPO_ROOT/src at import time
            export REPO_ROOT=''${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}
            [ -f "$REPO_ROOT/src/kb/__init__.py" ] \
              || echo "kb: run nix develop inside the checkout or export REPO_ROOT" >&2
          '';
        };
      });
}
