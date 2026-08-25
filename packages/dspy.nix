{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  wheel,
  anyio,
  asyncer,
  backoff,
  cachetools,
  cloudpickle,
  diskcache,
  joblib,
  json-repair,
  litellm,
  magicattr,
  numpy,
  openai,
  orjson,
  pydantic,
  regex,
  requests,
  rich,
  tenacity,
  tqdm,
  typeguard,
  ujson,
  xxhash,
  gepa,
}:
buildPythonPackage rec {
  pname = "dspy";
  version = "3.2.1";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "stanfordnlp";
    repo = "dspy";
    tag = version;
    hash = "sha256-xquV+FyDfejm1SCWYfuiezIkyutmm/1zOvd5X+oElrM=";
  };

  postPatch = ''
    substituteInPlace dspy/__metadata__.py \
      --replace-fail '__version__="3.2.0"' '__version__="${version}"'
    substituteInPlace pyproject.toml \
      --replace-fail 'version="3.2.0"' 'version="${version}"' \
      --replace-fail '"asyncer==0.0.8",' '"asyncer>=0.0.8",' \
      --replace-fail '"gepa[dspy]==0.0.27",' '"gepa[dspy]>=0.0.27",' \
      --replace-fail '"typeguard==4.4.3",' '"typeguard>=4.4.3",'
  '';

  build-system = [
    setuptools
    wheel
  ];

  dependencies = [
    anyio
    asyncer
    backoff
    cachetools
    cloudpickle
    diskcache
    gepa
    joblib
    json-repair
    litellm
    magicattr
    numpy
    openai
    orjson
    pydantic
    regex
    requests
    rich
    tenacity
    tqdm
    typeguard
    ujson
    xxhash
  ];

  doCheck = false;

  pythonImportsCheck = [ "dspy" ];

  meta = {
    description = "Framework for programming—not prompting—language models";
    homepage = "https://github.com/stanfordnlp/dspy";
    license = lib.licenses.mit;
  };
}
