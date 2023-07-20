# Copyright (c) 2023, healthcare and contributors
# For license information, please see license.txt

import json
from frappe import _
import frappe

import re

from frappe.model.document import Document
from frappe.utils import date_diff, getdate, now_datetime, today
from erpnext.setup.doctype.terms_and_conditions.terms_and_conditions import get_terms_and_conditions


class Observation(Document):
	def validate(self):
		self.set_age()
		self.set_result_time()
		self.set_status()
		age = frappe.utils.date_diff(frappe.utils.nowdate(), frappe.db.get_value("Patient", self.patient, "dob"))
		self.reference = get_observation_reference(self)
		self.validate_input()

	def before_insert(self):
		set_observation_idx(self)

	def set_age(self):
		patient_doc = frappe.get_doc("Patient", self.patient)
		self.age = patient_doc.calculate_age().get("age_in_string")
		# self.years = patient_doc.calculate_age()[1].get("years")
		# self.months = patient_doc.calculate_age()[1].get("months")
		self.days = patient_doc.calculate_age().get("age_in_days")

	def set_status(self):
		if self.has_result() and self.status != "Final":
			self.status = "Preliminary"
		elif self.amended_from and self.status not in ["Amended", "Corrected"]:
			self.status = "Amended"
		elif self.status not in ["Final", "Cancelled", "Entered in Error", "Unknown"]:
			self.status = "Registered"

	def set_result_time(self):
		if not self.time_of_result and self.has_result():
			self.time_of_result = now_datetime()
		else:
			self.time_of_result = ""

		if self.status == "Final" and not self.time_of_approval:
			self.time_of_approval = now_datetime()
		else:
			self.time_of_approval = ""

	def has_result(self):
		result_fields = [
			"result_attach",
			"result_boolean",
			"result_data",
			"result_text",
			"result_float",
			"result_select",
		]
		for field in result_fields:
			if self.get(field, None):
				return True

		# TODO: handle fields defaulting to now
		# "result_datetime",
		# "result_time",
		# "result_period_from",
		# "result_period_to",

		return False

	def validate_input(self):
		if self.permitted_data_type in ["Quantity", "Numeric"]:
			if self.result_data and not is_numbers_with_exceptions(self.result_data):
				frappe.throw(
					_(
						"Non numeric result {0} is not allowed for Permitted Data Type {1}"
					).format(
						frappe.bold(self.result_data), frappe.bold(self.permitted_data_type)
					)
				)


@frappe.whitelist()
def get_observation_details(docname):
	patient, gender, reference, reference_posting_date = frappe.get_value(
		"Diagnostic Report", docname, ["patient", "gender", "docname", "reference_posting_date"]
	)
	dob = frappe.db.get_value("Patient", patient, "dob")
	age = 0
	if dob:
		if not reference_posting_date:
			reference_posting_date = frappe.utils.nowdate()
		age = frappe.utils.date_diff(reference_posting_date, dob)

	observation = frappe.get_list(
		"Observation",
		fields=[
			"*"
		],
		filters={"sales_invoice": reference, "parent_observation": "", "status": ["!=", "Cancelled"]},
		order_by="creation",
	)
	out_data = []
	obs_length = 0
	for obs in observation:
		has_result = False
		if not obs.get("has_component"):
			if obs.get("permitted_data_type"):
				obs_length += 1
			observation_data = {}
			if obs.get("permitted_data_type") == "Select" and obs.get("options"):
				obs["options_list"] = obs.get("options").split("\n")
			if obs.get("observation_template"):
				if obs.get("specimen"):
					obs["received_time"] = frappe.get_value("Specimen", obs.get("specimen"), "received_time")
				out_data.append(observation_data)
				observation_data["observation"] = obs

		else:
			child_observations = frappe.get_list(
				"Observation",
				fields=[
					"*"
				],
				filters={"parent_observation": obs.get("name"), "status": ["!=", "Cancelled"]},
				order_by="observation_idx",
			)
			obs_list = []
			obs_dict = {}
			for child in child_observations:
				if child.get("permitted_data_type"):
					obs_length += 1
				if child.get("permitted_data_type") == "Select" and child.get("options"):
					child["options_list"] = child.get("options").split("\n")
				if child.get("specimen"):
					child["received_time"] = frappe.get_value("Specimen", child.get("specimen"), "received_time")
				observation_data = {}
				observation_data["observation"] = child
				obs_list.append(observation_data)
				if child.get("result_data") or child.get("result_text") or child.get("result_select")  not in [None, "", "Null"]:
					has_result = True
			if len(child_observations) > 0:
				obs_dict["has_component"] = True
				obs_dict["observation"] = obs.get("name")
				obs_dict[obs.get("name")] = obs_list
				obs_dict["display_name"] = obs.get("observation_template")
				obs_dict["practitioner_name"] = obs.get("practitioner_name")
				obs_dict["healthcare_practitioner"] = obs.get("healthcare_practitioner")
				obs_dict["description"] = obs.get("description")
				obs_dict["has_result"] = False
				if has_result:
					obs_dict["has_result"] = True
			out_data.append(obs_dict)

	return out_data, obs_length


def get_observation_reference(doc):
	template_doc = frappe.get_doc("Observation Template", doc.observation_template)
	display_reference = ""
	for child in template_doc.observation_reference_range:
		if not child.applies_to == "All":
			if not child.applies_to == doc.gender:
				continue
		if child.age == "Range":
			day_from = day_to = 0
			if child.from_age_type == "Months":
				day_from = float(child.age_from) * 30.436875
			elif child.from_age_type == "Years":
				day_from = float(child.age_from) * 365.2425
			elif child.from_age_type == "Days":
				day_from = float(child.age_from)

			if child.to_age_type == "Months":
				day_to = float(child.age_to) * 30.436875
			elif child.to_age_type == "Years":
				day_to = float(child.age_to) * 365.2425
			elif child.to_age_type == "Days":
				day_to = float(child.age_to)

			if float(day_from) <= float(doc.days) <= float(day_to):
				display_reference += set_reference_string(child)

		elif child.age == "All":
			display_reference += set_reference_string(child)

	return display_reference

def set_reference_string(child):
	display_reference = ""
	if (child.reference_from and child.reference_to) or child.conditions:
		if child.reference_from and child.reference_to:
			display_reference += str(child.reference_from) + "-" + str(child.reference_to)
		elif child.conditions:
			display_reference += str(child.conditions)
		else:
			display_reference += ""
		display_reference += ((": " + str(child.short_interpretation)) if child.short_interpretation else "") + "<br>"
	return display_reference


@frappe.whitelist()
def edit_observation(observation, data_type, result):
	observation_doc = frappe.get_doc("Observation", observation)
	if data_type in ["Range", "Ratio", "Quantity", "Numeric"]:
		observation_doc.result_data = result
	# elif data_type in ["Quantity", "Numeric"]:
	# 	observation_doc.result_float = result
	elif data_type == "Text":
		observation_doc.result_text = result
	observation_doc.save()


@frappe.whitelist()
def add_observation(
	patient,
	template,
	data_type=None,
	result=None,
	doc=None,
	docname=None,
	parent=None,
	specimen=None,
	invoice="",
):
	observation_doc = frappe.new_doc("Observation")
	observation_doc.posting_datetime = now_datetime()
	observation_doc.patient = patient
	observation_doc.observation_template = template
	observation_doc.permitted_data_type = data_type
	observation_doc.reference_doctype = doc
	observation_doc.reference_docname = docname
	observation_doc.sales_invoice = invoice
	observation_doc.specimen = specimen
	if data_type in ["Range", "Ratio", "Quantity", "Numeric"]:
		observation_doc.result_data = result
	# elif data_type in ["Quantity", "Numeric"]:
	# 	observation_doc.result_float = result
	elif data_type == "Text":
		observation_doc.result_text = result
	if parent:
		observation_doc.parent_observation = parent
	observation_doc.insert()
	return observation_doc.name


@frappe.whitelist()
def record_observation_result(values):
	values = json.loads(values)
	if values:
		for val in values:
			if not val.get("observation"):
				return
			observation_doc = frappe.get_doc("Observation", val["observation"])
			if observation_doc.get("permitted_data_type") in [
				"Range",
				"Ratio",
				"Quantity",
				"Numeric",
			]:
				if observation_doc.get("permitted_data_type") in [
					"Quantity",
					"Numeric",
				] and val.get("result") and not is_numbers_with_exceptions(val.get("result")):
					frappe.msgprint(
						_("Non numeric result {0} is not allowed for Permitted Type {1}").format(
							frappe.bold(val.get("result")),
							frappe.bold(observation_doc.get("permitted_data_type")),
						),
						indicator="orange",
						alert=True,
					)
					return

				if val.get("result") != observation_doc.get("result_data"):
					observation_doc.result_data = val.get("result")
					observation_doc.time_of_result = now_datetime()
					observation_doc.save()
			elif observation_doc.get("permitted_data_type") == "Text":
				if val.get("result") != observation_doc.get("result_text"):
					observation_doc.result_text = val.get("result")
					observation_doc.time_of_result = now_datetime()
					observation_doc.save()
			elif observation_doc.get("permitted_data_type") == "Select":
				if val.get("result") != observation_doc.get("result_select"):
					observation_doc.result_select = val.get("result")
					observation_doc.time_of_result = now_datetime()
					observation_doc.save()

			if observation_doc.get("observation_category") == "Imaging":
				if val.get("result"):
					observation_doc.result_text = val.get("result")
				if val.get("interpretation"):
					observation_doc.result_interpretation = val.get("interpretation")
				if val.get("result") or val.get("interpretation"):
					observation_doc.time_of_result = now_datetime()
					observation_doc.save()


@frappe.whitelist()
def add_note(note, observation):
	if note and observation:
		frappe.db.set_value("Observation", observation, "note", note)


def set_observation_idx(doc):
	if doc.parent_observation:
		parent_template = frappe.db.get_value("Observation", doc.parent_observation, "observation_template")
		idx = frappe.db.get_value("Observation Component", {"parent": parent_template, "observation_template":doc.observation_template}, "idx")
		if idx:
			doc.observation_idx = idx

def is_numbers_with_exceptions(value):
	pattern = r'^[0-9{}]+$'.format(re.escape(".<>"))
	return re.match(pattern, value) is not None

@frappe.whitelist()
def get_observation_result_template(template_name, observation):
	if observation:
		observation_doc = frappe.get_doc("Observation", observation)
		patient_doc = frappe.get_doc("Patient", observation_doc.get("patient"))
		observation_doc = json.loads(observation_doc.as_json())
		patient_doc = json.loads(patient_doc.as_json())
		# merged_dict = {"patient": patient_doc, "observation":observation_doc}
		merged_dict = {**observation_doc, **patient_doc}
		terms = get_terms_and_conditions(template_name, merged_dict)
	return terms
