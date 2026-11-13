# Security policy

Report a vulnerability privately through GitHub's [Report a vulnerability
form](https://github.com/Akutagawa1998/Zhong-Shu-OSS/security/advisories/new).
Include the kit version, affected command, expected behavior and a minimal
example with synthetic data. Keep credentials and live research records out
of reports and public issues.

The kit stores research memory locally. Keep live hubs and their Git remotes
private. Before sharing an export, review its contents, archive members and
any Git history it carries. The sync checks cover common credential patterns.
They cannot identify all sensitive research data or all credentials.

Run the kit from a trusted checkout or install. Review project Python plugins
before opting into them for a specific hub. Read commands execute enabled
plugins with the caller's permissions. Run logs can contain raw command
output.
