import QtQuick
import qs.Commons

// The bar reading ("CPU 23%"), sized for the widest label so samples never
// resize the island. Panel.qml puts it in its WidgetButton; the preview
// draws it on bar-coloured strips.
Item {
  id: root

  property string text: "CPU --"
  property bool warning: false
  property color foreground: "white"
  property color warningColor: "red"
  property string fontFamily: Style.font.family
  readonly property real labelWidth: Math.ceil(metrics.width)

  implicitWidth: labelWidth
  implicitHeight: Style.bar.iconCanvas

  TextMetrics {
    id: metrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    // Keep the reading close to its label while reserving three digits.
    font.wordSpacing: -Style.space(4)
    text: "RAM 100%"
  }

  Text {
    anchors.fill: parent
    textFormat: Text.PlainText
    text: root.text
    color: root.warning ? root.warningColor : root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    font.wordSpacing: metrics.font.wordSpacing
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    renderType: Text.NativeRendering
  }
}
