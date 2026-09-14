{ system ? builtins.currentSystem }: {
  hello = builtins.derivation { name = "hello-1.0"; inherit system; builder = "/bin/false"; };
  tools = { recurseForDerivations = true;
    multi = builtins.derivation { name = "multi-2.0"; inherit system; builder = "/bin/false"; outputs = [ "out" "dev" ]; };
  };
}
