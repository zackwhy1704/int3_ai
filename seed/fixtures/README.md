# Test fixture tenants (synthetic)

Used by the cross-tenant tests together with `seed/brindlewood` (tenant A).
Every string here is invented. Each document carries a unique marker string so a
test can prove it never reaches another tenant.

- `hollowmere` (tenant B): a different company. Its document ids deliberately
  collide with tenant A's (`refund-policy-v3`, `finance-annual-contract-terms`), and
  it has a `finance` scope with the same name as A's plus a `treasury` scope A lacks.
- `brindlewood_labs` (tenant C): shares tenant A's email domain
  (`brindlewood.example`). Domain overlap must not grant access either way.
