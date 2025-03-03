"""Minimalistic fork of readability-lxml code

This is a python port of a ruby port of arc90's readability project

http://lab.arc90.com/experiments/readability/

Given a html document, it pulls out the main body text and cleans it up.

Ruby port by starrhorne and iterationlabs
Python port by gfxmonk

For list of contributors see
https://github.com/timbertson/python-readability
https://github.com/buriy/python-readability

License of forked code: Apache-2.0 License
This code: GPLv3+
"""

import logging
import re

from lxml.etree import tostring
from lxml.html import fragment_fromstring

from .utils import trim


LOGGER = logging.getLogger(__name__)


BAD_ATTRS = ("|".join(["width", "height", "style", "[-a-z]*color", "background[-a-z]*", "on*"]))
QUOTES = '\'[^\']+\'|"[^"]+"'
NON_SPACE = "[^ \"'>]+"
HTMLSTRIP = re.compile(
    "<"  # open
    "([^>]+) "  # prefix
    "(?:%s) *" % BAD_ATTRS
    + "= *(?:%s|%s)"  # undesirable attributes
    % (NON_SPACE, QUOTES)
    + "([^>]*)"  # value  # postfix
    ">",  # end
    re.I,
)


def clean_attributes(html):
    while HTMLSTRIP.search(html):
        html = HTMLSTRIP.sub("<\\1\\2>", html)
    return html


def _tostring(string):
    return tostring(string, encoding=str, method='xml')  # method='text'


DIV_TO_P_ELEMS = {'a', 'blockquote', 'dl', 'div', 'img', 'ol', 'p', 'pre', 'table', 'ul'}

DIV_SCORES = {"div", "article"}
BLOCK_SCORES = {"pre", "td", "blockquote"}
BAD_ELEM_SCORES = {"address", "ol", "ul", "dl", "dd", "dt", "li", "form", "aside"}
STRUCTURE_SCORES = {"h1", "h2", "h3", "h4", "h5", "h6", "th", "header", "footer", "nav"}

TEXT_CLEAN_ELEMS = {"p", "img", "li", "a", "embed", "input"}

REGEXES = {
    "unlikelyCandidatesRe": re.compile(
        r"combx|comment|community|disqus|extra|foot|header|menu|remark|rss|shoutbox|sidebar|sponsor|ad-break|agegate|pagination|pager|popup|tweet|twitter",
        re.I,
    ),
    "okMaybeItsACandidateRe": re.compile(r"and|article|body|column|main|shadow", re.I),
    "positiveRe": re.compile(
        r"article|body|content|entry|hentry|main|page|pagination|post|text|blog|story",
        re.I,
    ),
    "negativeRe": re.compile(
        r"combx|comment|com-|contact|foot|footer|footnote|masthead|media|meta|outbrain|promo|related|scroll|shoutbox|sidebar|sponsor|shopping|tags|tool|widget",
        re.I,
    ),
    "divToPElementsRe": re.compile(
        r"<(a|blockquote|dl|div|img|ol|p|pre|table|ul)", re.I
    ),
    "videoRe": re.compile(r"https?:\/\/(www\.)?(youtube|vimeo)\.com", re.I),
}


def text_length(elem):
    return len(trim(elem.text_content())) or 0


class Candidate:
    "Defines a class to score candidate elements."
    __slots__ = ['score', 'elem']

    def __init__(self, score, elem):
        self.score = score
        self.elem = elem


class Document:
    """Class to build a etree document out of html."""
    __slots__ = ['doc', 'min_text_length', 'retry_length']

    def __init__(self, doc, min_text_length=25, retry_length=250):
        """Generate the document

        :param doc: string of the html content.
        :param min_text_length: Set to a higher value for more precise detection of longer texts.
        :param retry_length: Set to a lower value for better detection of very small texts.

        The Document class is not re-enterable.
        It is designed to create a new Document() for each HTML file to process it.

        API method:
        .summary() -- cleaned up content
        """
        self.doc = doc
        self.min_text_length = min_text_length
        self.retry_length = retry_length

    def get_clean_html(self):
        """
        An internal method, which can be overridden in subclasses, for example,
        to disable or to improve DOM-to-text conversion in .summary() method
        """
        return clean_attributes(_tostring(self.doc))

    def summary(self):
        """
        Given a HTML file, extracts the text of the article.

        Warning: It mutates internal DOM representation of the HTML document,
        so it is better to call other API methods before this one.
        """
        ruthless = True
        while True:
            for i in self.tags(self.doc, "script", "style"):
                i.drop_tree()
            for i in self.tags(self.doc, "body"):
                i.set("id", "readabilityBody")
            if ruthless:
                self.remove_unlikely_candidates()
            self.transform_misused_divs_into_paragraphs()
            candidates = self.score_paragraphs()

            best_candidate = self.select_best_candidate(candidates)

            if best_candidate is not None:
                article = self.get_article(candidates, best_candidate)
            else:
                if ruthless is True:
                    ruthless = False
                    LOGGER.debug("Ended up stripping too much - going for a safer parse")
                    # try again
                    continue
                # go ahead
                LOGGER.debug("Ruthless and lenient parsing did not work. Returning raw html")
                article = self.doc.find("body")
                if article is None:
                    article = self.doc

            cleaned_article = self.sanitize(article, candidates)
            article_length = len(cleaned_article or "")
            if ruthless is True and not article_length >= self.retry_length:
                ruthless = False
                # Loop through and try again.
                continue
            return cleaned_article

    def get_article(self, candidates, best_candidate):
        # Now that we have the top candidate, look through its siblings for
        # content that might also be related.
        # Things like preambles, content split by ads that we removed, etc.
        sibling_score_threshold = max(10, best_candidate.score * 0.2)
        # create a new html document with a div
        output = fragment_fromstring("<div/>")
        parent = best_candidate.elem.getparent()
        siblings = parent.getchildren() if parent is not None else [best_candidate.elem]
        for sibling in siblings:
            # in lxml there no concept of simple text
            # if isinstance(sibling, NavigableString): continue
            append = False
            # conditions
            if sibling == best_candidate.elem:
                append = True
            elif (
                sibling in candidates
                and candidates[sibling].score >= sibling_score_threshold
            ):
                append = True
            elif sibling.tag == "p":
                link_density = self.get_link_density(sibling)
                node_content = sibling.text or ""
                node_length = len(node_content)

                if node_length > 80 and link_density < 0.25:
                    append = True
                elif (
                    node_length <= 80
                    and link_density == 0
                    and re.search(r"\.( |$)", node_content)
                ):
                    append = True
            # append to the output div
            if append is True:
                output.append(sibling)
        #if output is not None:
        #    output.append(best_candidate.elem)
        return output

    def select_best_candidate(self, candidates):
        if not candidates:
            return None
        sorted_candidates = sorted(
            candidates.values(), key=lambda x: x.score, reverse=True
        )
        for candidate in sorted_candidates[:5]:
            LOGGER.debug("Top 5: %s %s", candidate.elem.tag, candidate.score)
        # return best candidate
        return sorted_candidates[0]

    def get_link_density(self, elem):
        total_length = text_length(elem) or 1
        link_length = sum(text_length(elem) for elem in elem.findall(".//a"))
        return link_length / total_length

    def score_paragraphs(self):
        candidates = {}
        ordered = []
        for elem in self.tags(self.doc, "p", "pre", "td"):
            parent_node = elem.getparent()
            if parent_node is None:
                continue
            grand_parent_node = parent_node.getparent()

            elem_text = trim(elem.text_content() or "")
            elem_text_len = len(elem_text)

            # don't count too short paragraphs
            if elem_text_len < self.min_text_length:
                continue

            if parent_node not in candidates:
                candidates[parent_node] = self.score_node(parent_node)
                ordered.append(parent_node)

            if grand_parent_node is not None and grand_parent_node not in candidates:
                candidates[grand_parent_node] = self.score_node(grand_parent_node)
                ordered.append(grand_parent_node)

            score = 1 + len(elem_text.split(",")) + min((elem_text_len / 100), 3)
            #if elem not in candidates:
            #    candidates[elem] = self.score_node(elem)

            candidates[parent_node].score += score
            if grand_parent_node is not None:
                candidates[grand_parent_node].score += score / 2

        # Scale the final candidates score based on link density. Good content
        # should have a relatively small link density (5% or less) and be
        # mostly unaffected by this operation.
        for elem in ordered:
            candidate = candidates[elem]
            density = self.get_link_density(elem)
            LOGGER.debug("Branch %6.3f link density %.3f -> %6.3f",
                candidate.score, density, candidate.score * (1 - density)
            )
            candidate.score *= 1 - density

        return candidates

    def class_weight(self, elem):
        weight = 0
        for attribute in (a for a in (elem.get("class"), elem.get("id")) if a is not None):
            if REGEXES["negativeRe"].search(attribute):
                weight -= 25
            if REGEXES["positiveRe"].search(attribute):
                weight += 25
        return weight

    def score_node(self, elem):
        score = self.class_weight(elem)
        name = elem.tag.lower()
        if name in DIV_SCORES:
            score += 5
        elif name in BLOCK_SCORES:
            score += 3
        elif name in BAD_ELEM_SCORES:
            score -= 3
        elif name in STRUCTURE_SCORES:
            score -= 5
        return Candidate(score, elem)

    def remove_unlikely_candidates(self):
        for elem in self.doc.findall(".//*"):
            attrs = ' '.join(a for a in (elem.get("class"), elem.get("id")) if a is not None)
            if len(attrs) < 2:
                continue
            if (
                REGEXES["unlikelyCandidatesRe"].search(attrs)
                and (not REGEXES["okMaybeItsACandidateRe"].search(attrs))
                and elem.tag not in ("html", "body")
            ):
                LOGGER.debug("Removing unlikely candidate: %s", elem.tag)
                elem.drop_tree()

    def transform_misused_divs_into_paragraphs(self):
        for elem in self.tags(self.doc, "div"):
            # transform <div>s that do not contain other block elements into
            # <p>s
            # FIXME: The current implementation ignores all descendants that
            # are not direct children of elem
            # This results in incorrect results in case there is an <img>
            # buried within an <a> for example
            ## TODO: if not any(e.tag in DIV_TO_P_ELEMS for e in list(elem)):
            if not REGEXES["divToPElementsRe"].search(
                ''.join(_tostring(e) for e in list(elem))
            ):
                elem.tag = "p"

        for elem in self.tags(self.doc, "div"):
            if elem.text is not None:
                elem_text = elem.text.strip()
                if elem_text:
                    p_elem = fragment_fromstring("<p/>")
                    p_elem.text = elem.text
                    elem.text = None
                    elem.insert(0, p_elem)

            for pos, child in sorted(enumerate(elem), reverse=True):
                if child.tail and child.tail.strip():
                    p_elem = fragment_fromstring("<p/>")
                    p_elem.text = child.tail
                    child.tail = None
                    elem.insert(pos + 1, p_elem)
                if child.tag == "br":
                    child.drop_tree()

    def tags(self, node, *tag_names):
        for tag_name in tag_names:
            for elem in node.findall(".//%s" % tag_name):
                yield elem

    def reverse_tags(self, node, *tag_names):
        for tag_name in tag_names:
            for elem in reversed(node.findall(".//%s" % tag_name)):
                yield elem

    def sanitize(self, node, candidates):
        for header in self.tags(node, "h1", "h2", "h3", "h4", "h5", "h6"):
            if self.class_weight(header) < 0 or self.get_link_density(header) > 0.33:
                header.drop_tree()

        for elem in self.tags(node, "form", "textarea"):
            elem.drop_tree()

        for elem in self.tags(node, "iframe"):
            if "src" in elem.attrib and REGEXES["videoRe"].search(elem.attrib["src"]):
                elem.text = "VIDEO"  # ADD content to iframe text node to force <iframe></iframe> proper output
            else:
                elem.drop_tree()

        allowed = set()
        # Conditionally clean <table>s, <ul>s, and <div>s
        for elem in self.reverse_tags(
            node, "table", "ul", "div", "aside", "header", "footer", "section"
        ):
            if elem in allowed:
                continue
            weight = self.class_weight(elem)
            if elem in candidates:
                score = candidates[elem].score
            else:
                score = 0

            if weight + score < 0:
                LOGGER.debug("Removed %s with score %6.3f and weight %-3s",
                    elem.tag, score, weight
                )
                elem.drop_tree()
            elif elem.text_content().count(",") < 10:
                to_remove = False
                counts = {}
                for kind in TEXT_CLEAN_ELEMS:
                    counts[kind] = len(elem.findall(".//%s" % kind))
                counts["li"] -= 100
                counts["input"] -= len(elem.findall('.//input[@type="hidden"]'))

                # Count the text length excluding any surrounding whitespace
                content_length = text_length(elem)
                link_density = self.get_link_density(elem)
                parent_node = elem.getparent()
                if parent_node is not None:
                    if parent_node in candidates:
                        score = candidates[parent_node].score
                    else:
                        score = 0

                # if elem.tag == 'div' and counts["img"] >= 1:
                #    continue
                if counts["p"] and counts["img"] > 1 + counts["p"] * 1.3:
                    reason = "too many images (%s)" % counts["img"]
                    to_remove = True
                elif counts["li"] > counts["p"] and elem.tag not in ("ol", "ul"):
                    reason = "more <li>s than <p>s"
                    to_remove = True
                elif counts["input"] > (counts["p"] / 3):
                    reason = "less than 3x <p>s than <input>s"
                    to_remove = True
                elif content_length < self.min_text_length and counts["img"] == 0:
                    reason = (
                        "too short content length %s without a single image"
                        % content_length
                    )
                    to_remove = True
                elif content_length < self.min_text_length and counts["img"] > 2:
                    reason = (
                        "too short content length %s and too many images"
                        % content_length
                    )
                    to_remove = True
                elif weight < 25 and link_density > 0.2:
                    reason = "too many links %.3f for its weight %s" % (
                        link_density,
                        weight,
                    )
                    to_remove = True
                elif weight >= 25 and link_density > 0.5:
                    reason = "too many links %.3f for its weight %s" % (
                        link_density,
                        weight,
                    )
                    to_remove = True
                elif (counts["embed"] == 1 and content_length < 75) or counts[
                    "embed"
                ] > 1:
                    reason = (
                        "<embed>s with too short content length, or too many <embed>s"
                    )
                    to_remove = True
                elif not content_length:
                    reason = "no content"
                    to_remove = True

                    # find x non empty preceding and succeeding siblings
                    siblings = []
                    for sib in elem.itersiblings():
                        sib_content_length = text_length(sib)
                        if sib_content_length:
                            siblings.append(sib_content_length)
                            if len(siblings) >= 1:
                                break
                    limit = len(siblings) + 1
                    for sib in elem.itersiblings(preceding=True):
                        sib_content_length = text_length(sib)
                        if sib_content_length:
                            siblings.append(sib_content_length)
                            if len(siblings) >= limit:
                                break
                    if siblings and sum(siblings) > 1000:
                        to_remove = False
                        for desnode in self.tags(elem, "table", "ul", "div", "section"):
                            allowed.add(desnode)

                if to_remove:
                    LOGGER.debug("Removed %6.3f %s with weight %s cause it has %s.",
                        score, elem.tag, weight, reason or ""
                    )
                    elem.drop_tree()
                else:
                    LOGGER.debug("Not removing %s of length %s",
                        elem.tag, content_length
                    )

        self.doc = node
        return self.get_clean_html()
