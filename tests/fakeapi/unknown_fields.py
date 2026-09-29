"""Handlers for the unknown_fields fixture. Each handler echoes the request body it
received, so tests can assert which data the business function saw."""


def reject(body):
    return body


def keep(body):
    return body


def strip(body):
    return body


def strip_composed(body):
    return body


def strip_oneof(body):
    return body
