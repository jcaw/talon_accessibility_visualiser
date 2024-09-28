import threading
from queue import LifoQueue
from typing import List

from talon import ui, canvas, Module, Context, app, skia, ctrl, actions, cron, clip
from talon.ui import Point2d, Rect


if app.platform == "windows":
    TYPEFACE = "Consolas"
else:
    TYPEFACE = "monospace"


class ElementNotFoundError(RuntimeError):
    pass


class Element(object):
    def __init__(self, e: ui.Element):
        self.rect = e.rect
        try:
            self.text = f'Name: "{e.name}": Class: "{e.class_name}"'
        except OSError:
            self.text = str(e)

    def __str__(self):
        return f"<Element: <{self.text}>, {self.rect}>"


elements_list = []
mouse_pos = Point2d(0, 0)
is_searching_tree = False
elements_list_lock = threading.Lock()


def get_text_components(elements, is_searching_tree_=False):
    text_components = []
    for i, element in enumerate(elements):
        if is_searching_tree_:
            component = "Finding ancestors for element: " + str(element)
        else:
            if len(elements) == 1:
                prefix = "──"
            elif i == len(elements) - 1:
                prefix = "└──"
            elif i > 0:
                prefix = "└┬─"
            else:
                prefix = "┬─"
            component = " " * (i - 1) + prefix + str(element)
        text_components.append(component)
    return text_components


def draw(c: canvas.Canvas):
    paint = c.paint
    with elements_list_lock:
        elements_list_ = elements_list
        mouse_pos_ = Point2d(*mouse_pos)
        is_searching_tree_ = is_searching_tree

    smallest_dim = min(c.width, c.height)
    paint.textsize = int(max(round(smallest_dim / 64), 5))
    x_padding = paint.textsize
    y_padding = paint.textsize
    paint.antialias = True
    paint.typeface = TYPEFACE
    paint.font.embolden = False

    text_components = []
    row_width = 0
    row_height = 0
    text_components = get_text_components(elements_list_, is_searching_tree_)
    for component in text_components:
        # HACK: `paint.measure_text` doesn't measure spaces, so replace them.
        dims = paint.measure_text(component.replace(" ", "-"))[1]
        row_width = max(row_width, dims.width)
        row_height = max(row_height, dims.height)
    padded_row_height = row_height * 1.05

    if mouse_pos_.x > c.rect.x + c.rect.width / 2:
        x = c.rect.x + x_padding
    else:
        # TODO: Shift properly to the right
        x = c.rect.x + c.rect.width - row_width - x_padding
    if mouse_pos_.y < c.rect.y + c.rect.height / 2:
        # Have to offset with height because of where the text is drawn.
        base_y = (
            c.rect.y
            + c.rect.height
            - padded_row_height * (len(text_components) - 1)
            - y_padding
        )
    else:
        # TODO: Shift properly to the bottom
        # HACK: Reduce the row height since it doesn't take into account full
        #   text height
        # TODO: Calculate the actual row height offset - it isn't offset by the
        #   full height, it's offset only by the height of the main character
        #   body.
        base_y = c.rect.y + row_height * 0.8 + y_padding

    if is_searching_tree:
        box_stroke = "#00FFFF"
        text_color = "#55FFFF"
    else:
        box_stroke = "#FF0000"
        # Looks orange unless we offset the balance
        # text_color = "#FF4466"
        text_color = "#FF7788"
    box_fill = box_stroke + "07"

    # Draw the bounding boxes of the element and its ancestors
    paint.style = paint.Style.STROKE
    paint.stroke_width = 2
    for element in elements_list_:
        rrect = skia.RoundRect.from_rect(element.rect, x=3, y=3)
        paint.style = paint.Style.STROKE
        paint.color = box_stroke
        c.draw_rrect(rrect)
        paint.style = paint.Style.FILL
        paint.color = box_fill
        c.draw_rrect(rrect)

    # Draw the background box for the text
    paint.style = paint.Style.FILL
    paint.color = "#DEFE"
    background_rect = Rect(
        x - x_padding,
        base_y - row_height * 0.8 - y_padding,
        row_width + x_padding * 2,
        padded_row_height * len(text_components) + y_padding * 2,
    )
    c.draw_rect(background_rect)
    paint.style = paint.Style.STROKE
    paint.color = "#000B"
    c.draw_rect(background_rect)

    # Draw the text itself, the path to the element.
    paint.stroke_width = 3
    paint.style = paint.Style.STROKE
    paint.color = "#000000"
    for i, text in enumerate(text_components):
        c.draw_text(text, x, base_y + i * padded_row_height)
    paint.style = paint.Style.FILL
    paint.color = text_color
    for i, text in enumerate(text_components):
        c.draw_text(text, x, base_y + i * padded_row_height)


module = Module()
module.tag("visualiser_active", "Active when the UI accessibility visualiser is active")

visualiser_active_context = Context()
canvases = []


def create_canvases():
    # destroy_canvases()
    if not canvases:
        for screen in ui.screens():
            c = canvas.Canvas.from_screen(screen)
            # HOTFIX: from_screen not working right on Windows
            if app.platform == "windows":
                hotfix_rect = Rect(*screen.rect)
                hotfix_rect.height -= 1
                c.rect = hotfix_rect
            c.focusable = False
            c.register("draw", draw)
            c.freeze()
            canvases.append(c)
    visualiser_active_context.tags = ["user.visualiser_active"]


def destroy_canvases():
    visualiser_active_context.tags = []
    for c in canvases:
        c.unregister("draw", draw)
        c.close()
    canvases.clear()


def redraw_canvases():
    for c in canvases:
        c.resume()
        c.freeze()


def same_element(a, b):
    """Do two accessibility elements appear to be the same element?"""
    try:
        a_handle = a.window_handle
    except OSError:
        a_handle = None
    try:
        b_handle = b.window_handle
    except OSError:
        b_handle = None
    handles_match = a_handle == b_handle
    try:
        return (
            handles_match
            and a.name == b.name
            and a.class_name == b.class_name
            and a.rect == b.rect
            and a.patterns == b.patterns
            and a.automation_id == b.automation_id
            # TODO: Also compare all children? That could even be recursive but then
            #  need a more efficient structure so we only do it once.
        )
    except OSError:
        return False


def find_ancestors_slow(element):
    # HACK: Manually scrape the tree to find an element
    print("Finding ancestors, slow. This may take a while.")
    queue = LifoQueue()
    # TODO: Get windows, *then* get the elements from the windows.
    # TODO: Sort the windows so browsers come last
    # queue.put((ui.root_element(), []))
    windows = []
    browser_windows = []
    for window in ui.windows():
        if window.hidden or window.minimized:
            continue
        # TODO: Full browser names
        for browser in {"firefox", "edge", "google chrome", "safari", "brave"}:
            if browser in window.app.name.lower():
                browser_windows.append(window)
                continue
        windows.append(window)
    # Browser windows usually take a long time to scrape, so do them last.
    windows.extend(browser_windows)

    # Filter out the matching windows only
    try:
        element_window_handle = element.window_handle
    except OSError:
        element_window_handle = None
    for window in reversed(windows):
        try:
            window_element = window.element
        except OSError:
            continue
        if not element_window_handle:
            queue.put((window_element, []))
            continue
        try:
            window_handle = window_element.window_handle
            if window_handle == element_window_handle:
                queue.put((window_element, []))
        except OSError:
            queue.put((window_element, []))

    while not queue.empty():
        current, ancestors = queue.get()
        # if current.window_handle != element.window_handle:
        #     # Prune dud windows
        #     continue
        children = list(current.children)
        for child in children:
            # TODO: Fix the comparison
            if same_element(element, child):
                return [Element(e) for e in [*ancestors, current, child]]
        for child in children:
            queue.put((child, [*ancestors, current, child]))
    raise ElementNotFoundError(f"`{element}` could not be found in ui tree.")


def find_ancestors_fast(element):
    elements = [element]
    while True:
        # NOTE: This method seems to only exist on Mac
        parent = element.parent
        if not parent:
            break
        elements.append(Element(parent))
        element = parent
    return reversed(elements)


@module.action_class
class Actions:
    def visualiser_gather_at_point():
        """Find and show the ui element heirarchy at point."""
        global elements_canvas, elements_list, mouse_pos, is_searching_tree

        mouse_pos_ = ctrl.mouse_pos()
        create_canvases()

        base_element = ui.element_at(*mouse_pos_)

        if base_element:
            elements = [Element(base_element)]

            # Show the element while it's loading the ancestors
            with elements_list_lock:
                # TODO: Maybe show this element in a different color?
                elements_list = elements
                mouse_pos = Point2d(*mouse_pos_)
                is_searching_tree = True
            redraw_canvases()

            # `Element.parent` doesn't seem to exist on Windows - only seems to
            # exist on Mac. The fast method requires it.
            #
            # (This approach of checking for the method will fail if it's just
            # stubbed on Windows in a future Talon release.)
            if hasattr(ui.Element, "parent"):
                elements = find_ancestors_fast(base_element)
            else:
                try:
                    elements = find_ancestors_slow(base_element)
                except ElementNotFoundError:
                    pass
        else:
            elements = []

        with elements_list_lock:
            elements_list = elements
            mouse_pos = Point2d(*mouse_pos_)
            is_searching_tree = False
        redraw_canvases()

    # TODO: Just roll this into the gather at point function?
    def visualiser_gather_at_point_and_copy():
        """Copy the ancestor path to the element at point, as text."""
        actions.self.visualiser_gather_at_point()
        with elements_list_lock:
            elements = elements_list
        clip.set_text("\n".join(get_text_components(elements)))
        app.notify(
            "Talon", "Accessibility element ancestry information copied to clipboard."
        )

    def visualiser_close():
        """Destroy the visualiser canvases, and exit the visualiser."""
        destroy_canvases()


# cron.interval("5s", actions.self.visualiser_gather_at_point)
