#!/bin/bash
source /home/marwan/Programming/odoo/venv/bin/activate
cd /home/marwan/Programming/odoo/odoo_17
python3 odoo-bin -c odoo.conf --dev=all "$@"
