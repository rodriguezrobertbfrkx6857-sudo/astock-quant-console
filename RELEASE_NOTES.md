# Public framework snapshot

The public snapshot is separate from the private installation. Included: frontend/backend source, research and reference engines, schemas, tests, generic configuration, installation instructions, CI, MIT license and third-party notices.

Excluded: private configuration and keys, paid/downloaded market data, holdings and account records, OOS exposure history, research outputs, manifests tied to private datasets, logs, internal reports, editor/agent state, Python environments, npm dependencies, generated frontend bundles, and Git history.

Portability changes: relative compatibility-engine paths resolve against the project root; machine-specific protected roots are removed while writes remain confined to the repository; relay addresses use reserved `.invalid` examples and adapters are disabled by default; the V2 credential compatibility entry point reads only the explicit environment variable.

The public initializer installs both control and accounting schemas without creating accounts or publishing synthetic data. The operational profile is a pending example, not evidence of successful research or PAPER qualification.

Validation cleanup in this snapshot: a missing-column guard prevents the short-term feature extractor from raising KeyError; validation fixtures isolate the intended error and use reproducible random data. The original private installation was not changed.

Default tests cover source-only behavior. Historical contract/integration tests are included for operators with their own prepared data; they are outside the default test selection. No full-market or live-provider acceptance is claimed.
