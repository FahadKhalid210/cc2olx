import logging
import re
import urllib
import xml.dom.minidom
from lxml import html
from cc2olx.iframe_link_parser import KalturaIframeLinkParser

from cc2olx.qti import QtiExport
from cc2olx.utils import element_builder

logger = logging.getLogger()


class OlxExportException(Exception):
    """
    Exception type for errors during exporting normalized
    Common Cartridge data to OLX format.
    """


class OlxExport:
    """
    This class is used to convert intermediate representation
    of Common Cartridge to OLX.

    OLX guide: https://edx.readthedocs.io/projects/edx-open-learning-xml/en/latest/
    """

    # content types
    HTML = "html"
    LINK = "link"
    VIDEO = "video"
    LTI = "lti"
    QTI = "qti"
    DISCUSSION = "discussion"

    def __init__(self, cartridge, link_file=None):
        self.cartridge = cartridge
        self.doc = None
        self.link_file = link_file
        self.iframe_link_parser = None
        if link_file:
            self.iframe_link_parser = KalturaIframeLinkParser(self.link_file)

    def xml(self):
        self.doc = xml.dom.minidom.Document()
        self.doc.appendChild(self.doc.createComment(" Generated by cc2olx "))

        xcourse = self.doc.createElement("course")
        xcourse.setAttribute("org", self.cartridge.get_course_org())
        xcourse.setAttribute("course", "Some_cc_Course")
        xcourse.setAttribute("name", self.cartridge.get_title())
        self.doc.appendChild(xcourse)

        tags = "chapter sequential vertical".split()
        self._add_olx_nodes(xcourse, self.cartridge.normalized["children"], tags)

        return self.doc.toprettyxml()

    def _add_olx_nodes(self, element, course_data, tags):
        """
        Recursively loops through the normalized common cartridge course data and
        adds appropriate OLX nodes to given course element.

        Expects `course_data` to be a list of triple nested elements that
        represent chapters in OLX courseware structure, like:
        ```
        [
            {
                'children': [        <----- chapter
                    'children': [        <----- sequential
                        'children': [        <----- vertical
                            ...content of vertical...
                        ]
                    ]
                ]
            }
        ]
        ```
        """

        leaf = not tags
        for element_data in course_data:
            if leaf:
                content_type, details = self._get_content(element_data)
                children = self._create_olx_nodes(content_type, details)
            else:
                children = [self.doc.createElement(tags[0])]

            for child in children:
                if "title" in element_data:
                    child.setAttribute("display_name", element_data["title"])
                    child.setAttribute("url_name", element_data["identifierref"])

                element.appendChild(child)

                if "children" in element_data:
                    self._add_olx_nodes(child, element_data["children"], tags[1:])

    def _get_content(self, element_data):
        """
        Gets content type and details from element's data.
        """

        content_type = None
        details = None

        if "identifierref" in element_data:
            idref = element_data["identifierref"]
            content_type, details = self.cartridge.get_resource_content(idref)

        if content_type is None:
            content_type = self.HTML
            details = {
                "html": "<p>MISSING CONTENT</p>",
            }

        if content_type == self.LINK:
            content_type, details = process_link(details)

        return content_type, details

    def _process_static_links(self, html):
        """
        Process static links like src and href to have appropriate links.
        """
        items = re.findall(r'(src|href)\s*=\s*"(.+?)"', html)

        def process_wiki_reference(item, html):
            """
            Replace $WIKI_REFERENCE$ with edx /jump_to_id/<url_name>
            """
            search_key = urllib.parse.unquote(item).replace("$WIKI_REFERENCE$/pages/", "")

            # remove query params and add suffix .html to match with resource_id_by_href
            search_key = search_key.split("?")[0] + ".html"
            for key in self.cartridge.resource_id_by_href.keys():
                if key.endswith(search_key):
                    replace_with = "/jump_to_id/{}".format(self.cartridge.resource_id_by_href[key])
                    html = html.replace(item, replace_with)
                    return html
            logger.warn("Unable to process Wiki link - %s", item)
            return html

        def process_ims_cc_filebase(item, html):
            """
            Replace $IMS-CC-FILEBASE$ with /static
            """
            new_item = urllib.parse.unquote(item).replace("$IMS-CC-FILEBASE$", "/static")
            # skip query parameters for static files
            new_item = new_item.split("?")[0]
            # &amp; is not valid in an URL. But some file seem to have it when it should be &
            new_item = new_item.replace("&amp;", "&")
            html = html.replace(item, new_item)
            return html

        for _, item in items:
            if "IMS-CC-FILEBASE" in item:
                html = process_ims_cc_filebase(item, html)
            elif "WIKI_REFERENCE" in item:
                html = process_wiki_reference(item, html)

        return html

    def _create_olx_nodes(self, content_type, details):
        """
        This helps to create OLX node of different type. For eg HTML, VIDEO, QTI, LTI,
        Discussion.

        Args:
            content_type ([str]): The type of node that has to be created.
            details (Dict[str, str]): Dictionary of the element and content of the element.

        Raises:
            OlxExportException: Exception when nodes are not able to be created.

        Returns:
            [List]: List of OLX nodes that needs to be written.
        """

        nodes = []

        if content_type == self.HTML:
            nodes += self._process_html(details)

        elif content_type == self.VIDEO:
            nodes += self._create_video_node(details)

        elif content_type == self.LTI:
            nodes.append(self._create_lti_node(details))

        elif content_type == self.QTI:
            qti_export = QtiExport(self.doc)
            nodes += qti_export.create_qti_node(details)

        elif content_type == self.DISCUSSION:
            nodes += self._create_discussion_node(details)

        else:
            raise OlxExportException(f'Content type "{content_type}" is not supported.')

        return nodes

    def _create_video_node(self, details):
        """
        This function creates Video OLX nodes.

        Args:
            details (Dict[str, str]): Dictionary that has Video tag value.

        Returns:
            [OLX Element]: Video OLX element.
        """
        xml_element = element_builder(self.doc)
        attributes = {
            "youtube": "1.00:" + details["youtube"],
            "youtube_id_1_0": details["youtube"]
        }
        child = xml_element("video", children=None, attributes=attributes)
        return child

    def _process_html(self, details):
        """
        This function helps to process the html and gives out
        corresponding HTML or Video OLX nodes.

        Args:
            details (Dict[str, str]): Dictionary that has HTML tag value.

        Returns:
            List[OLX Element]: List of html/Video OLX element.
        """
        video_olx = []
        nodes = []
        child = self.doc.createElement("html")
        html = self._process_static_links(details["html"])
        if self.link_file:
            html, video_olx = self._process_html_for_iframe(html)
        txt = self.doc.createCDATASection(html)
        child.appendChild(txt)
        nodes.append(child)
        for olx in video_olx:
            nodes.append(olx)
        return nodes

    def _process_html_for_iframe(self, html_str):
        """
        This function helps to parse the iframe with
        embedded video, to be converted into video xblock.

        Args:
            html_str ([str]): Html file content.

        Returns:
            html_str [str]: The html content of the file, if iframe is present
                            and converted into xblock then iframe is removed
                            from the HTML.
            video_olx [List[xml]]: List of xml children, i.e video xblock.
        """
        video_olx = []
        parsed_html = html.fromstring(html_str)
        iframes = parsed_html.xpath("//iframe")
        if not iframes:
            return html_str, video_olx
        video_olx, converted_iframes = self.iframe_link_parser.get_video_olx(self.doc, iframes)
        if video_olx:
            # If video xblock is present then we modify the HTML to remove the iframe
            # hence we need to convert the modified HTML back to string.
            for iframe in converted_iframes:
                iframe.getparent().remove(iframe)
            return html.tostring(parsed_html).decode('utf-8'), video_olx
        return html_str, video_olx

    def _create_lti_node(self, details):
        node = self.doc.createElement("lti_consumer")
        custom_parameters = "[{params}]".format(
            params=", ".join(
                [
                    '"{key}={value}"'.format(
                        key=key,
                        value=value,
                    )
                    for key, value in details["custom_parameters"].items()
                ]
            ),
        )
        node.setAttribute("custom_parameters", custom_parameters)
        node.setAttribute("description", details["description"])
        node.setAttribute("display_name", details["title"])
        node.setAttribute("inline_height", details["height"])
        node.setAttribute("inline_width", details["width"])
        node.setAttribute("launch_url", details["launch_url"])
        node.setAttribute("modal_height", details["height"])
        node.setAttribute("modal_width", details["width"])
        node.setAttribute("xblock-family", "xblock.v1")
        return node

    def _create_discussion_node(self, details):
        node = self.doc.createElement("discussion")
        node.setAttribute("display_name", "")
        node.setAttribute("discussion_category", details["title"])
        node.setAttribute("discussion_target", details["title"])
        html_node = self.doc.createElement("html")
        txt = self.doc.createCDATASection(details["text"])
        html_node.appendChild(txt)
        return [html_node, node]


def process_link(details):
    """
    Possibly convert a link to a video.
    """

    # YouTube links can be like this: https://www.youtube.com/watch?v=gQ-cZRmHfs4&amp;amp;list=PL5B350D511278A56B
    ytmatch = re.search(r"youtube.com/watch\?v=([-\w]+)", details["href"])
    if ytmatch:
        return "video", {"youtube": ytmatch.group(1)}

    details = {
        "html": "<a href='{}'>{}</a>".format(details["href"], details.get("text", "")),
    }

    return "html", details
