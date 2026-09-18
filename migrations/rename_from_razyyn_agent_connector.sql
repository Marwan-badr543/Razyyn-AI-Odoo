-- Rename an installed `razyyn_agent_connector` to `razyyn_ai`, keeping data.
--
-- WHY THIS EXISTS
--   The module was renamed to match the product. Odoo identifies an installed
--   module by its directory name, so without this it sees `razyyn_ai` as a
--   brand-new module and `razyyn_agent_connector` as one whose code has
--   vanished. Installing the new one beside the old leaves two modules owning
--   the same models; uninstalling the old one first DROPS its tables — every
--   chat session, every row of the write audit log, and the API key the
--   customer's Razyyn connection authenticates with.
--
--   The models themselves do not change name (`razyyn.agent.settings` is still
--   `razyyn.agent.settings`), so their tables and every row in them are
--   already correct. All that has to move is the OWNERSHIP records: which
--   module Odoo believes installed each one.
--
-- WHEN TO RUN IT
--   Once, against the customer's database, with Odoo STOPPED, before starting
--   it with `-u razyyn_ai`. Skip it entirely for a fresh install — there is
--   nothing to rename.
--
--   Safe to run twice: every statement is scoped to the old name, so a second
--   run matches nothing.
--
--     psql -d <database> -f rename_from_razyyn_agent_connector.sql

BEGIN;

-- The module record itself. `latest_version` is cleared so Odoo runs the new
-- code's own upgrade path rather than believing it is already current.
UPDATE ir_module_module
   SET name = 'razyyn_ai'
 WHERE name = 'razyyn_agent_connector';

-- Every XML id this module created: views, menus, actions, access rules,
-- cron jobs. Odoo matches these by module name, and an external id still
-- filed under the old module is one the new code cannot find — so it creates
-- a SECOND copy of every view and menu instead of updating the first.
UPDATE ir_model_data
   SET module = 'razyyn_ai'
 WHERE module = 'razyyn_agent_connector';

-- Dependency rows, so other modules that required this one still resolve.
UPDATE ir_module_module_dependency
   SET name = 'razyyn_ai'
 WHERE name = 'razyyn_agent_connector';

COMMIT;
