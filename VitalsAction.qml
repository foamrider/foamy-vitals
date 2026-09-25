import QtQuick
import qs.Commons
import qs.Ui

Rectangle {
  id: root
  property string iconName: "settings"
  property string label: ""
  property bool spinning: false
  property string tooltipText: ""
  property real iconSize: Style.space(15)
  property Item keyTarget: null
  property color foreground: Color.foreground
  property string fontFamily: "sans-serif"
  signal clicked()
  implicitWidth: label ? content.implicitWidth + Style.space(12) : Style.space(26)
  implicitHeight: Style.space(28)
  radius: Style.space(6)
  opacity: enabled ? 1 : 0.4
  color: mouse.containsMouse || activeFocus ? Qt.rgba(foreground.r, foreground.g, foreground.b, 0.08) : "transparent"
  activeFocusOnTab: true
  border.width: activeFocus ? 1 : 0
  border.color: Color.accent
  Accessible.role: Accessible.Button
  Accessible.name: tooltipText || label
  Accessible.onPressAction: if (enabled) clicked()
  Keys.onReturnPressed: if (enabled) clicked()
  Keys.onEnterPressed: if (enabled) clicked()
  Keys.onSpacePressed: if (enabled) clicked()
  Keys.forwardTo: keyTarget ? [keyTarget] : []
  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.space(7)
    VitalsIcon {
      id: actionIcon
      visible: root.iconName !== ""
      width: root.iconSize
      height: width
      color: root.foreground
      name: root.iconName
      anchors.verticalCenter: parent.verticalCenter
      // Animate only the glyph; the hover and focus surface stays stationary.
      RotationAnimation on rotation {
        running: root.spinning
        from: 0; to: 360; duration: 800; loops: Animation.Infinite
        onStopped: actionIcon.rotation = 0
      }
    }
    Text { visible: root.label !== ""; text: root.label; textFormat: Text.PlainText; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.space(11) }
  }
  MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: root.clicked() }
  PanelToolTip {
    visible: mouse.containsMouse && root.tooltipText !== ""
    text: root.tooltipText
    fontFamily: root.fontFamily
  }
}
