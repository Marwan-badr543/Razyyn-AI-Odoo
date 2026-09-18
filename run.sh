#!/bin/bash
source /home/marwan/Programming/odoo/odoo_18/odoo-venv/bin/activate
cd /home/marwan/Programming/odoo/odoo_18/odoo
python3 odoo-bin -c odoo.conf --dev=all "$@"
