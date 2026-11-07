# Deployment sources

Shared build templates and the public-feed Compose template are in
`utils/engine/deploy/`; the collector Dockerfile is beside its shared sources.
Historical model-specific templates remain with their declared legacy model.
Account configuration, persistent volumes and credentials remain operationally
separate. No image build, service activation, server migration or deployment occurred.
