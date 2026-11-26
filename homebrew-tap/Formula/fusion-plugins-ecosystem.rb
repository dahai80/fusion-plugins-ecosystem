class FusionPluginsEcosystem < Formula
    desc "Fusion plugin ecosystem: registry, lifecycle, MCP server, Claude adaptation"
    homepage "https://github.com/dahai80/fusion-plugins-ecosystem"
    url "https://github.com/dahai80/fusion-plugins-ecosystem/archive/refs/tags/v0.12.1.tar.gz"
    sha256 "bee593d5cdcfc91ecd43ca7ae096df96ec7f256add3bd24569934883274a8fd8"
    license "Apache-2.0"
    head "https://github.com/dahai80/fusion-plugins-ecosystem.git", branch: "main"

    depends_on "python@3.12"

    include Language::Python::Virtualenv

    # Install strategy: every dependency is a pinned wheel resource (brew
    # downloads all resources during the network-enabled fetch phase), then
    # the install step does a single offline `pip install --no-deps` of the
    # staged wheels. This avoids both virtualenv_install_with_resources'
    # --no-binary=:all: (which breaks platform wheels like pydantic-core,
    # cryptography, cffi) and any index access inside the build sandbox.
    # The dependency set is regenerated from a pip resolution report on
    # dependency changes (see homebrew-tap/ in the source repo).

    resource "fusion-plugins-ecosystem" do
        url "https://github.com/dahai80/fusion-plugins-ecosystem/releases/download/v0.12.1/fusion_plugins_ecosystem-0.12.1-py3-none-any.whl"
        sha256 "6825095118f69009fda6d9ece845704aa68cf763bfe791b6d1b30d3b6a986a43"
    end

    resource "annotated-doc" do
        url "https://files.pythonhosted.org/packages/cd/f5/3dd85d583e55be76cd02b1fc9bbec771b12d63124b01661d9535ccf8721f/annotated_doc-0.0.6-py3-none-any.whl"
        sha256 "361ca0950cc7082f53fa69e9328409f40a62c61e57e0a47e1825a08078a58a53"
    end

    resource "annotated-types" do
        url "https://files.pythonhosted.org/packages/99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/annotated_types-0.8.0-py3-none-any.whl"
        sha256 "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0"
    end

    resource "anyio" do
        url "https://files.pythonhosted.org/packages/a9/16/7757fb84c819accc7d7e9af5291b261e4abb8627ee040f99e7d35ef2457a/anyio-4.16.0-py3-none-any.whl"
        sha256 "c66ae634e0b8228396b801741beb3294326d4b6102f7bba4041c0dfb58c29026"
    end

    resource "attrs" do
        url "https://files.pythonhosted.org/packages/64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/attrs-26.1.0-py3-none-any.whl"
        sha256 "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309"
    end

    resource "certifi" do
        url "https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl"
        sha256 "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"
    end

    resource "cffi" do
        url "https://files.pythonhosted.org/packages/54/7d/16e5a096677b5e313ca80cd5e5170efa3ea44624a82bb111925522da64b1/cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "f81b3b8f3d4e343550fa4baa0e479bba9f2d29ce9c2e9b51d1ce1718d7442fcf"
    end

    resource "click" do
        url "https://files.pythonhosted.org/packages/31/7d/164b7ffc8624fe9efb4221bf9af485d5c1e41b9ae118045d2b6dc5457d8b/click-8.5.1-py3-none-any.whl"
        sha256 "8bb5376d2006950386f8c6427613334a2acadc5f559cb80e638d00b4df957e37"
    end

    resource "cryptography" do
        url "https://files.pythonhosted.org/packages/e5/56/d194340cc4a57535e82e1bee9e89667ac4b7c13b5d3f59686deae3094dd5/cryptography-50.0.2-cp311-abi3-macosx_11_0_arm64.whl"
        sha256 "fa8f5efb344d6908a1ce62f4a24e2e5780f825d6f53f5f50ec5ffacac72936cb"
    end

    resource "fastapi" do
        url "https://files.pythonhosted.org/packages/20/b6/2424da1e40825d3fc54539621811f34e21472499677c9490e4967270e38e/fastapi-0.163.1-py3-none-any.whl"
        sha256 "68f2d8050f5c3583121c0d203046cc7def5c16a7d6c43a0e7707a8ea610fe2d3"
    end

    resource "fusion-core" do
        url "https://files.pythonhosted.org/packages/55/13/8e71cdf8aa367f00b9c2ccde432e190df5b0636ebe7fd4d388f0b6778874/fusion_core-0.12.1-py3-none-any.whl"
        sha256 "547f1600fa57bf32d75482ae6364b9a8c60a48ac65684e80c7dc3c6e59ede034"
    end

    resource "h11" do
        url "https://files.pythonhosted.org/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl"
        sha256 "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"
    end

    resource "httpcore" do
        url "https://files.pythonhosted.org/packages/7e/f5/f66802a942d491edb555dd61e3a9961140fd64c90bce1eafd741609d334d/httpcore-1.0.9-py3-none-any.whl"
        sha256 "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"
    end

    resource "httpx" do
        url "https://files.pythonhosted.org/packages/2a/39/e50c7c3a983047577ee07d2a9e53faf5a69493943ec3f6a384bdc792deb2/httpx-0.28.1-py3-none-any.whl"
        sha256 "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"
    end

    resource "idna" do
        url "https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl"
        sha256 "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c"
    end

    resource "jsonschema" do
        url "https://files.pythonhosted.org/packages/69/90/f63fb5873511e014207a475e2bb4e8b2e570d655b00ac19a9a0ca0a385ee/jsonschema-4.26.0-py3-none-any.whl"
        sha256 "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce"
    end

    resource "jsonschema-specifications" do
        url "https://files.pythonhosted.org/packages/41/45/1a4ed80516f02155c51f51e8cedb3c1902296743db0bbc66608a0db2814f/jsonschema_specifications-2025.9.1-py3-none-any.whl"
        sha256 "98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe"
    end

    resource "opentelemetry-api" do
        url "https://files.pythonhosted.org/packages/a9/2d/610db864d7513158cebb8f36cc88e67e73bd6c7aa1905ef18d89e1281c81/opentelemetry_api-1.45.2-py3-none-any.whl"
        sha256 "2cbd737377e710ee740d261c94e93fa0f08e6d037c18f17513cc0a2d94479a34"
    end

    resource "pycparser" do
        url "https://files.pythonhosted.org/packages/b6/ae/315bbeee126a769cbde43cf4908acb4b40408d8bf8bbf334dd7368d8dadf/pycparser-3.12-py3-none-any.whl"
        sha256 "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e"
    end

    resource "pydantic" do
        url "https://files.pythonhosted.org/packages/72/b8/b864503789ef5c58597d2a7b4adbee62c26440ab1e682581fc6838df8947/pydantic-2.14.3-py3-none-any.whl"
        sha256 "3f7b347782ab7e4cf4c798496ae3a82787d9f21ec12676a9c564355eeef8daa8"
    end

    resource "pydantic-core" do
        url "https://files.pythonhosted.org/packages/c1/39/6618e541658d5a687a8d4962fc304350073a8f322bc4c62e8ed4c2d514d6/pydantic_core-2.50.4-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "1e20cbde2b6deb33a51d08cd168454dd9044ec7d4474056f4d98d63f51578783"
    end

    resource "referencing" do
        url "https://files.pythonhosted.org/packages/2c/58/ca301544e1fa93ed4f80d724bf5b194f6e4b945841c5bfd555878eea9fcb/referencing-0.37.0-py3-none-any.whl"
        sha256 "381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231"
    end

    resource "rpds-py" do
        url "https://files.pythonhosted.org/packages/90/81/ac6a0d064982251856ce009c9e1dd51a34110b3c055aa7d1aad18b4899a3/rpds_py-2026.9.1-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "e21c1429e205828ea886a2293a4a2c8e01f4c25d9893ca330e97a6cf73f52e7b"
    end

    resource "setproctitle" do
        url "https://files.pythonhosted.org/packages/57/be/6a10d95bba71fd5f441dda01ae374ebcaeaa637a09f72b95e8cc59443958/setproctitle-1.3.8-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "823d7d8790a4f8484c7fa65468fcd9d009f13ffe5f0bc89ec0e0d2c165ac0335"
    end

    resource "starlette" do
        url "https://files.pythonhosted.org/packages/88/2e/947cfe02def9371c07cd5bf5b6b61c829e9aecb9904db02a66cfafbc1eec/starlette-1.10.0-py3-none-any.whl"
        sha256 "478c0ab3b4ced6e82719849c118fa406f0278a3499c1a898cd15cd31bdc38ce4"
    end

    resource "typing-extensions" do
        url "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl"
        sha256 "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"
    end

    resource "typing-inspection" do
        url "https://files.pythonhosted.org/packages/de/0c/ea2544850a7c135e15033a5c7a8e7211136184b7ae63e2239d557c2b3019/typing_inspection-0.4.5-py3-none-any.whl"
        sha256 "bc210535442b39e1bf2805e0c7a17dfd6a4ace760c84c7f7d16245d23a6b1c39"
    end

    resource "uvicorn" do
        url "https://files.pythonhosted.org/packages/38/0c/b54a4fdd7f90a3af8b02ebc9ce6712c2c208b7926a2f7bad95c33ebbe943/uvicorn-0.54.0-py3-none-any.whl"
        sha256 "505bdb0f318731d45f1f712071fc781a8981f6847a31c902c9f5e652d4f67faf"
    end

    def install
        venv = virtualenv_create(libexec, "python3.12")
        # Resource#stage unpacks archives (a wheel is a zip), so install from
        # the cached downloads (original .whl files). Brew cache filenames
        # carry a "<sha>--" prefix that pip rejects as a wheel filename —
        # copy each file under its clean basename first.
        wheel_dir = buildpath/"wheels"
        wheels = resources.map do |r|
            name = File.basename(r.cached_download.to_s).sub(/\A[0-9a-f]{64}--/, "")
            dst = wheel_dir/name
            dst.dirname.mkpath
            cp r.cached_download, dst
            dst
        end
        system Formula["python@3.12"].opt_bin/"python3.12", "-m", "pip",
               "--python=#{libexec}/bin/python", "install", "--no-deps", *wheels
        bin.install libexec/"bin/fusion-plugin-server"
        bin.install libexec/"bin/fusion-plugin-console"
    end

    def caveats
        <<~EOS
            MCP server (stdio for Claude Desktop / Claude Code):
              fusion-plugin-server --transport stdio

            MCP server (HTTP):
              fusion-plugin-server --transport http --port 8765

            Extensions console:
              fusion-plugin-console --port 8795

            Data is stored in ~/.fusion-plugins-ecosystem/
        EOS
    end

    test do
        assert_match version.to_s, shell_output("#{bin}/fusion-plugin-server --version")
        assert_match version.to_s, shell_output("#{bin}/fusion-plugin-console --version")
    end
end
