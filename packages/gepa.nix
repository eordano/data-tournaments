{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  wheel,
  litellm,
  datasets,
  tqdm,
}:
buildPythonPackage rec {
  pname = "gepa";
  version = "0.1.4";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "gepa-ai";
    repo = "gepa";
    tag = "v${version}";
    hash = "sha256-s9/Vjzd5/JFuMgT9huiURu6I8qlxsRagi4h6v+75IxM=";
  };

  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace-fail 'version="0.1.3"' 'version="${version}"'
  '';

  build-system = [
    setuptools
    wheel
  ];

  dependencies = [
    litellm
    datasets
    tqdm
  ];

  doCheck = false;

  pythonImportsCheck = [ "gepa" ];

  meta = {
    description = "Reflective prompt evolution via LLM-based reflection and Pareto-efficient evolutionary search";
    homepage = "https://github.com/gepa-ai/gepa";
    license = lib.licenses.mit;
  };
}
