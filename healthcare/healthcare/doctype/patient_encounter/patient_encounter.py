# -*- coding: utf-8 -*-
# Copyright (c) 2015, ESS LLP and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import add_days, getdate

from healthcare.healthcare.utils import get_medical_codes


class PatientEncounter(Document):
	def validate(self):
		self.set_title()
		validate_codification_table(self)
		self.validate_medications()

	def on_update(self):
		if self.appointment:
			frappe.db.set_value("Patient Appointment", self.appointment, "status", "Closed")

	def on_submit(self):
		if self.therapies:
			create_therapy_plan(self)

		self.make_service_request()
		self.make_medication_request()

	def before_cancel(self):
		orders = frappe.get_all("Service Request", {"order_group": self.name})
		for order in orders:
			order_doc = frappe.get_doc("Service Request", order.name)
			if order_doc.docstatus == 1:
				order_doc.cancel()

	def on_cancel(self):
		if self.appointment:
			frappe.db.set_value("Patient Appointment", self.appointment, "status", "Open")

		if self.inpatient_record and self.drug_prescription:
			delete_ip_medication_order(self)

	def set_title(self):
		self.title = _("{0} with {1}").format(
			self.patient_name or self.patient, self.practitioner_name or self.practitioner
		)[:100]

	@staticmethod
	@frappe.whitelist()
	def get_applicable_treatment_plans(encounter):
		patient = frappe.get_doc("Patient", encounter["patient"])

		plan_filters = {}
		plan_filters["name"] = ["in", []]

		age = patient.age
		if age:
			plan_filters["patient_age_from"] = ["<=", age.years]
			plan_filters["patient_age_to"] = [">=", age.years]

		gender = patient.sex
		if gender:
			plan_filters["gender"] = ["in", [gender, None]]

		diagnosis = encounter.get("diagnosis")
		if diagnosis:
			diagnosis = [_diagnosis["diagnosis"] for _diagnosis in encounter["diagnosis"]]
			filters = [
				["diagnosis", "in", diagnosis],
				["parenttype", "=", "Treatment Plan Template"],
			]
			diagnosis = frappe.db.get_all("Patient Encounter Diagnosis", filters=filters, fields="*")
			plan_names = [_diagnosis["parent"] for _diagnosis in diagnosis]
			plan_filters["name"][1].extend(plan_names)

		symptoms = encounter.get("symptoms")
		if symptoms:
			symptoms = [symptom["complaint"] for symptom in encounter["symptoms"]]
			filters = [
				["complaint", "in", symptoms],
				["parenttype", "=", "Treatment Plan Template"],
			]
			symptoms = frappe.db.get_all("Patient Encounter Symptom", filters=filters, fields="*")
			plan_names = [symptom["parent"] for symptom in symptoms]
			plan_filters["name"][1].extend(plan_names)

		if not plan_filters["name"][1]:
			plan_filters.pop("name")

		plans = frappe.get_list("Treatment Plan Template", fields="*", filters=plan_filters)

		return plans

	@frappe.whitelist()
	def set_treatment_plans(self, treatment_plans=None):
		for treatment_plan in treatment_plans:
			self.set_treatment_plan(treatment_plan)

	def set_treatment_plan(self, plan):
		plan_doc = frappe.get_doc("Treatment Plan Template", plan)

		for plan_item in plan_doc.items:
			self.set_treatment_plan_item(plan_item)

		for drug in plan_doc.drugs:
			self.append("drug_prescription", (frappe.copy_doc(drug)).as_dict())

	def set_treatment_plan_item(self, plan_item):
		if plan_item.type == "Clinical Procedure Template":
			self.append("procedure_prescription", {"procedure": plan_item.template})

		if plan_item.type == "Lab Test Template":
			self.append("lab_test_prescription", {"lab_test_code": plan_item.template})

		if plan_item.type == "Therapy Type":
			self.append(
				"therapies",
				{"therapy_type": plan_item.template, "no_of_sessions": plan_item.qty},
			)

	def validate_medications(self):
		if not self.drug_prescription:
			return

		for item in self.drug_prescription:
			if not item.medication and not item.drug_code:
				frappe.throw(
					_("Row #{0} (Drug Prescription): Medication or Item Code is mandatory").format(item.idx)
				)

	def validate_therapies(self):
		if not self.therapies:
			return

		for therapy in self.therapies:
			if therapy.get_quantity() <= 0:
				frappe.throw(
					_("Row #{0} (Therapies): Number of Sessions should be at least 1").format(therapy.idx)
				)

	def make_service_request(self):
		if self.lab_test_prescription:
			for lab_test in self.lab_test_prescription:
				lab_template = frappe.get_doc("Lab Test Template", lab_test.lab_test_code)
				order = self.get_order_details(lab_template, lab_test)
				order.insert(ignore_permissions=True, ignore_mandatory=True)

		if self.procedure_prescription:
			for procedure in self.procedure_prescription:
				procedure_template = frappe.get_doc("Clinical Procedure Template", procedure.procedure)
				order = self.get_order_details(procedure_template, procedure)
				order.insert(ignore_permissions=True, ignore_mandatory=True)

		if self.therapies:
			for therapy in self.therapies:
				therapy_type = frappe.get_doc("Therapy Type", therapy.therapy_type)
				order = self.get_order_details(therapy_type, therapy)
				order.insert(ignore_permissions=True, ignore_mandatory=True)

		if self.observations:
			for observation in self.observations:
				if not observation.service_request:
					template = frappe.get_doc("Observation Template", observation.observation_template)
					order = self.get_order_details(template, observation)
					order.insert(ignore_permissions=True, ignore_mandatory=True)
					order.submit()
					observation.service_request = order.name

	def make_medication_request(self):
		if self.drug_prescription:
			# make_medication_request
			for drug in self.drug_prescription:
				if drug.medication:
					medication = frappe.get_doc("Medication", drug.medication)
					order = self.get_order_details(medication, drug, True)
					order.insert(ignore_permissions=True, ignore_mandatory=True)

	def get_order_details(self, template_doc, line_item, medication_request=False):
		order = frappe.get_doc(
			{
				"doctype": "Medication Request" if medication_request else "Service Request",
				"order_date": self.encounter_date,
				"order_time": self.encounter_time,
				"company": self.company,
				"status": "Draft",
				"patient": self.get("patient"),
				"practitioner": self.practitioner,
				"order_group": self.name,
				"sequence": line_item.get("sequence"),
				"patient_care_type": template_doc.get("patient_care_type"),
				"intent": line_item.get("intent"),
				"priority": line_item.get("priority"),
				"quantity": line_item.get_quantity() if line_item.doctype == "Drug Prescription" else 1,
				"dosage": line_item.get("dosage"),
				"dosage_form": line_item.get("dosage_form"),
				"period": line_item.get("period"),
				"expected_date": line_item.get("expected_date"),
				"as_needed": line_item.get("as_needed"),
				"staff_role": template_doc.get("staff_role"),
				"note": line_item.get("note"),
				"patient_instruction": line_item.get("patient_instruction"),
			}
		)

		if template_doc.doctype == "Lab Test Template":
			description = template_doc.get("lab_test_description")
		else:
			description = template_doc.get("description")

		if medication_request:
			order.update(
				{
					"medication": template_doc.name,
					"number_of_repeats_allowed": line_item.get("number_of_repeats_allowed"),
				}
			)
		else:
			order.update({"template_dt": template_doc.doctype, "template_dn": template_doc.name})

		order.update({"order_description": description})
		return order


@frappe.whitelist()
def make_ip_medication_order(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.start_date = source.encounter_date
		for entry in source.drug_prescription:
			if entry.drug_code:
				dosage = frappe.get_doc("Prescription Dosage", entry.dosage)
				dates = get_prescription_dates(entry.period, target.start_date)
				for date in dates:
					for dose in dosage.dosage_strength:
						order = target.append("medication_orders")
						order.drug = entry.drug_code
						order.drug_name = entry.drug_name
						order.dosage = dose.strength
						order.instructions = entry.comment
						order.dosage_form = entry.dosage_form
						order.date = date
						order.time = dose.strength_time
				target.end_date = dates[-1]

	doc = get_mapped_doc(
		"Patient Encounter",
		source_name,
		{
			"Patient Encounter": {
				"doctype": "Inpatient Medication Order",
				"field_map": {
					"name": "patient_encounter",
					"patient": "patient",
					"patient_name": "patient_name",
					"patient_age": "patient_age",
					"inpatient_record": "inpatient_record",
					"practitioner": "practitioner",
					"start_date": "encounter_date",
				},
			}
		},
		target_doc,
		set_missing_values,
	)

	return doc


def get_prescription_dates(period, start_date):
	prescription_duration = frappe.get_doc("Prescription Duration", period)
	days = prescription_duration.get_days()
	dates = [start_date]
	for i in range(1, days):
		dates.append(add_days(getdate(start_date), i))
	return dates


def create_therapy_plan(encounter):
	if len(encounter.therapies):
		doc = frappe.new_doc("Therapy Plan")
		doc.patient = encounter.patient
		doc.start_date = encounter.encounter_date
		for entry in encounter.therapies:
			doc.append(
				"therapy_plan_details",
				{"therapy_type": entry.therapy_type, "no_of_sessions": entry.no_of_sessions},
			)
		doc.save(ignore_permissions=True)
		if doc.get("name"):
			encounter.db_set("therapy_plan", doc.name)
			frappe.msgprint(
				_("Therapy Plan {0} created successfully.").format(frappe.bold(doc.name)), alert=True
			)


def delete_ip_medication_order(encounter):
	record = frappe.db.exists("Inpatient Medication Order", {"patient_encounter": encounter.name})
	if record:
		frappe.delete_doc("Inpatient Medication Order", record, force=1)


def validate_codification_table(doc):
	if doc.diagnosis:
		doc.codification_table = []
		for diag in doc.diagnosis:
			medical_code_details = get_medical_codes("Diagnosis", diag.diagnosis)
			if medical_code_details and len(medical_code_details) > 0:
				for m_code in medical_code_details:
					doc.append(
						"codification_table",
						{
							"medical_code": m_code.get("medical_code"),
							"medical_code_standard": m_code.get("medical_code_standard"),
							"code": m_code.get("code"),
							"description": m_code.get("description"),
							"system": m_code.get("system"),
						},
					)


@frappe.whitelist()
def create_service_request(encounter):
	encounter_doc = frappe.get_doc("Patient Encounter", encounter)
	if not frappe.db.exists("Service Request", {"order_group": encounter}):
		encounter_doc.make_service_request()

		for lab_presc in encounter_doc.lab_test_prescription:
			if lab_presc.invoiced:
				frappe.db.set_value(
					"Service Request",
					{"order_group": encounter, "template_dn": lab_presc.lab_test_code},
					{"docstatus": 1, "invoiced": 1, "status": "Active"},
				)

		for proc_presc in encounter_doc.procedure_prescription:
			if proc_presc.invoiced:
				frappe.db.set_value(
					"Service Request",
					{"order_group": encounter, "template_dn": proc_presc.procedure},
					{"docstatus": 1, "invoiced": 1, "status": "Active"},
				)


@frappe.whitelist()
def create_medication_request(encounter):
	encounter_doc = frappe.get_doc("Patient Encounter", encounter)
	if not frappe.db.exists("Medication Request", {"order_group": encounter}):
		encounter_doc.make_medication_request()


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_medications_query(doctype, txt, searchfield, start, page_len, filters):
	medication_name = filters.get("name")
	medication_child = frappe.qb.DocType("Medication Linked Item")
	medication = frappe.qb.DocType("Medication")
	item = frappe.qb.DocType("Item")
	data = (
		frappe.qb.select(medication_child.brand, medication_child.manufacturer, medication_child.item)
		.from_(medication_child)
		.left_join(medication)
		.on(medication.name == medication_child.parent)
		.left_join(item)
		.on(item.name == medication_child.item)
		.where((medication.name == medication_name) & (item.disabled == 0))
	).run(as_dict=True)
	data_list = []
	for d in data:
		display_list = []
		if d.get("item"):
			display_list.append(d.get("item"))
		if d.get("brand"):
			display_list.append(d.get("brand"))
		if d.get("manufacturer"):
			display_list.append(d.get("manufacturer"))
		default_warehouse = frappe.get_cached_value("Stock Settings", None, "default_warehouse")
		if default_warehouse:
			actual_qty = frappe.db.get_value(
				"Bin", {"warehouse": default_warehouse, "item_code": d.get("name")}, "actual_qty"
			)
			display_list.append("Qty:" + str(actual_qty) if actual_qty else "0")
		data_list.append(display_list)
	res = tuple(tuple(sub) for sub in data_list)
	return res


@frappe.whitelist()
def get_medications(medication):
	return frappe.get_all("Medication Linked Item", {"parent": medication}, ["item"])
