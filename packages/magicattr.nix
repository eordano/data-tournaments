{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  wheel,
}:
buildPythonPackage rec {
  pname = "magicattr";
  version = "0.1.6";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "frmdstryr";
    repo = "magicattr";
    tag = "v${version}";
    hash = "sha256-hV425AnXoYL3oSYMhbXaF8VRe/B1s5f5noAZYz4MMwc=";
  };

  build-system = [
    setuptools
    wheel
  ];

  doCheck = false;

  pythonImportsCheck = [ "magicattr" ];

  meta = {
    description = "getattr/setattr that works on nested objects without eval";
    homepage = "https://github.com/frmdstryr/magicattr";
    license = lib.licenses.mit;
  };
}
