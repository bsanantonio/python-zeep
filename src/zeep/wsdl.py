import pprint
from collections import namedtuple

import requests
from lxml.etree import QName

from zeep import xsd
from zeep.parser import parse_xml
from zeep.types import Schema
from zeep.utils import findall_multiple_ns, get_qname, parse_qname

NSMAP = {
    'xsd': 'http://www.w3.org/2001/XMLSchema',
    'wsdl': 'http://schemas.xmlsoap.org/wsdl/',
    'soap': 'http://schemas.xmlsoap.org/wsdl/soap/',
    'soap12': 'http://schemas.xmlsoap.org/wsdl/soap12/',
}


PortOperation = namedtuple('PortOperation', ['input', 'output', 'fault'])


class Message(object):
    def __init__(self, name):
        self.name = name
        self.parts = {}

    def add_part(self, name, element):
        self.parts[name] = element

    def get_part(self, name):
        return self.parts[name]


class Port(object):
    def __init__(self, name):
        self.name = name
        self.operations = {}

    def add_operation(self, name, input_message=None, output_message=None,
                      fault_message=None):

        self.operations[name] = PortOperation(
            input_message, output_message, fault_message)

    def get(self, name):
        return self.operations[name]


class Binding(object):
    def __init__(self, name, port_type):
        self.name = name
        self.port_type = port_type
        self.operations = {}

    def add_operation(self, name, soapaction, style):
        operation = BindingOperation(self, name, soapaction, style)
        self.operations[name.text] = operation
        return operation

    def get(self, name):
        return self.operations[name]


class BindingOperation(object):

    def __init__(self, binding, name, soapaction, style):
        self.name = name
        self.port_type = binding.port_type.get(name)
        self.soapaction = soapaction
        self.style = style
        self.protocol = {}

    def protocol_info(self, type_, use, namespace):
        self.protocol[type_] = {
            'namespace': namespace,
            'use': use
        }

    @property
    def input(self):
        return self.port_type.input

    @property
    def output(self):
        return self.port_type.output

    @property
    def fault(self):
        return self.port_type.fault



class WSDL(object):
    def __init__(self, filename):
        self.types = {}
        self.schema_references = {}

        if filename.startswith(('http://', 'https://')):
            response = requests.get(filename)
            doc = parse_xml(response.content, self.schema_references)
        else:
            with open(filename) as fh:
                doc = parse_xml(fh.read(), self.schema_references)

        self.nsmap = doc.nsmap
        self.target_namespace = doc.get('targetNamespace')
        self.types = self.parse_types(doc)
        self.messages = self.parse_messages(doc)
        self.ports = self.parse_ports(doc)
        self.bindings = self.parse_binding(doc)
        self.services = self.parse_service(doc)

    def dump(self):
        print "Types: "
        pprint.pprint(self.types)
        print "Messages: "
        pprint.pprint(self.messages)
        print "Ports: "
        pprint.pprint(self.ports)
        print "Bindings: "
        pprint.pprint(self.bindings)
        print "Services: "
        pprint.pprint(self.services)

    def parse_types(self, doc):
        namespace_sets = [
            {'xsd': 'http://www.w3.org/2001/XMLSchema'},
            {'xsd': 'http://www.w3.org/1999/XMLSchema'},
        ]

        types = doc.find('wsdl:types', namespaces=NSMAP)

        schema_nodes = findall_multiple_ns(types, 'xsd:schema', namespace_sets)
        if not schema_nodes:
            return Schema()

        for schema_node in schema_nodes:
            tns = schema_node.get('targetNamespace')
            self.schema_references['intschema+%s' % tns] = schema_node

        # Only handle the import statements from the 2001 xsd's for now
        import_tag = QName('http://www.w3.org/2001/XMLSchema', 'import').text
        for schema_node in schema_nodes:
            for import_node in schema_node.findall(import_tag):
                if import_node.get('schemaLocation'):
                    continue
                namespace = import_node.get('namespace')
                import_node.set('schemaLocation', 'intschema+%s' % namespace)

        return Schema(schema_nodes[0], self.schema_references)

    def parse_messages(self, doc):
        result = {}
        tns = doc.get('targetNamespace')

        messages = doc.findall("wsdl:message", namespaces=NSMAP)
        for message in messages:
            msg = Message(name=get_qname(message, 'name', tns, as_text=False))

            for part in message.findall('wsdl:part', namespaces=NSMAP):
                part_name = get_qname(part, 'name', tns, as_text=False)

                part_element = get_qname(part, 'element', tns)
                if part_element is not None:
                    part_type = self.types.get_element(part_element)
                else:
                    part_type = get_qname(part, 'type', tns)
                    part_type = self.types.get_type(part_type)
                    part_type = xsd.Element(part_name, type_=part_type())

                msg.add_part(part_name, part_type)
            result[msg.name.text] = msg

        return result

    def parse_ports(self, doc):
        findall = lambda node, name: node.findall(name, namespaces=NSMAP)
        tns = doc.get('targetNamespace')

        result = {}
        for port_node in findall(doc, 'wsdl:portType'):
            port_name = get_qname(port_node, 'name', tns, as_text=False)
            port = Port(port_name)

            for op_node in findall(port_node, 'wsdl:operation'):
                operation_name = get_qname(op_node, 'name', tns, as_text=False)
                messages = {}

                for type_ in 'input', 'output', 'fault':
                    msg_node = op_node.find('wsdl:%s' % type_, namespaces=NSMAP)
                    if msg_node is None:
                        continue
                    key = '%s_message' % type_
                    message_name = get_qname(msg_node, 'message', tns)
                    messages[key] = self.messages[message_name]
                port.add_operation(operation_name, **messages)
            result[port.name.text] = port

        return result

    def parse_binding(self, doc):
        findall = lambda node, name: node.findall(name, namespaces=NSMAP)
        tns = doc.get('targetNamespace')
        bindings = {}

        for binding_node in findall(doc, 'wsdl:binding'):

            name = get_qname(binding_node, 'name', tns, as_text=False)
            port_name = get_qname(binding_node, 'type', tns)
            port_type = self.ports[port_name]

            binding = Binding(name, port_type)

            # The soap:binding element contains the transport method and
            # default style attribute for the operations.
            soap_node = get_soap_node(binding_node, 'binding')
            transport = soap_node.get('transport')
            if transport != 'http://schemas.xmlsoap.org/soap/http':
                raise NotImplementedError("Only soap/http is supported for now")
            default_style = soap_node.get('style', 'document')

            for operation_node in findall(binding_node, 'wsdl:operation'):
                operation_name = get_qname(
                    operation_node, 'name', tns, as_text=False)

                # The soap:operation element is required for soap/http bindings
                # and may be omitted for other bindings.
                soap_node = get_soap_node(operation_node, 'operation')
                action = None
                if soap_node is not None:
                    action = soap_node.get('soapAction')
                    style = soap_node.get('style', default_style)
                else:
                    style = default_style

                operation = binding.add_operation(operation_name, action, style)

                for type_ in 'input', 'output', 'fault':
                    type_node = operation_node.find(QName(NSMAP['wsdl'], type_))
                    if type_node is None:
                        continue
                    soap_node = get_soap_node(type_node, 'body')
                    operation.protocol_info(
                        type_,
                        use=soap_node.get('use'),
                        namespace=soap_node.get('namespace'))

            bindings[binding.name.text] = binding

        return bindings

    def parse_service(self, doc):
        findall = lambda node, name: node.findall(name, namespaces=NSMAP)
        tns = doc.get('targetNamespace')

        result = {}
        for service_node in findall(doc, 'wsdl:service'):
            name = service_node.get('name')
            result[name] = {}

            for port_node in findall(service_node, 'wsdl:port'):
                soap_node = get_soap_node(port_node, 'address')
                binding = get_qname(port_node, 'binding', tns)
                port_name = get_qname(port_node, 'name', tns)

                result[name][port_name] = {
                    'binding': self.bindings[binding],
                    'address': soap_node.get('location'),
                }
        return result


def get_soap_node(parent, name):
    for ns in ['soap', 'soap12']:
        node = parent.find('%s:%s' % (ns, name), namespaces=NSMAP)
        if node is not None:
            return node
