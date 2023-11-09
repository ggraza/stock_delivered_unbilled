import frappe
from frappe import _
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost_sl_entries, repost_gl_entries, notify_error_to_stock_managers, _get_directly_dependent_vouchers
from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
from erpnext.accounts.utils import get_future_stock_vouchers, repost_gle_for_stock_vouchers, _delete_accounting_ledger_entries
from erpnext.stock.stock_ledger import (
	get_affected_transactions,
	get_items_to_be_repost,
	repost_future_sle,
)


def repost(doc):
	try:
		if not frappe.db.exists("Repost Item Valuation", doc.name):
			return

		# This is to avoid TooManyWritesError in case of large reposts
		frappe.db.MAX_WRITES_PER_TRANSACTION *= 4

		doc.set_status("In Progress")
		if not frappe.flags.in_test:
			frappe.db.commit()

		repost_sl_entries(doc)
		repost_gl_entries(doc)
		_post_affected_sales_invoices(doc)

		doc.set_status("Completed")

	except Exception as e:
		if frappe.flags.in_test:
			# Don't silently fail in tests,
			# there is no reason for reposts to fail in CI
			raise

		frappe.db.rollback()
		traceback = frappe.get_traceback()
		doc.log_error("Unable to repost item valuation")

		message = frappe.message_log.pop() if frappe.message_log else ""
		if traceback:
			message += "<br>" + "Traceback: <br>" + traceback
		frappe.db.set_value(doc.doctype, doc.name, "error_log", message)

		outgoing_email_account = frappe.get_cached_value(
			"Email Account", {"default_outgoing": 1, "enable_outgoing": 1}, "name"
		)

		if outgoing_email_account and not isinstance(e, RecoverableErrors):
			notify_error_to_stock_managers(doc, message)
			doc.set_status("Failed")
	finally:
		if not frappe.flags.in_test:
			frappe.db.commit()

def _post_affected_sales_invoices(doc):
	directly_dependent_transactions = _get_directly_dependent_vouchers(doc)
	repost_affected_transaction = get_affected_transactions(doc)
	all_affected_transactions = directly_dependent_transactions + list(repost_affected_transaction)
	affected_invoices = []
	for affected_transaction in all_affected_transactions:
		document_type, document_name = affected_transaction
		if document_type == "Delivery Note":
			invoice_list = frappe.get_list("Sales Invoice Item", fields=["name", "parent"], filters={"delivery_note": document_name})
			for invoice in invoice_list:
				docstatus = frappe.db.get_value("Sales Invoice", invoice.parent, "docstatus")
				if docstatus == 1:
					affected_invoices.append(invoice.parent)

	for inv in affected_invoices:
		voucher_obj = frappe.get_doc("Sales Invoice", inv)
		expected_gle = toggle_debit_credit_if_negative(voucher_obj.get_gl_entries())
		_delete_accounting_ledger_entries("Sales Invoice", inv)
		voucher_obj.make_gl_entries(gl_entries=expected_gle, from_repost=True)
