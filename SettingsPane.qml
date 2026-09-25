import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui
import "Preferences.js" as Preferences

Column {
  id: root
  required property var settings
  required property string language
  property bool saving: false
  property string error: ""
  property alias backTarget: backButton
  signal save(string key, var value)
  signal back()
  signal clearError()
  function tr(label) { return Preferences.text(label, language) }
  function focusBack() { backButton.forceActiveFocus() }
  readonly property color secondary: Qt.tint(Color.popups.background, Qt.alpha(Color.popups.text, 0.7))
  padding: Style.space(20)
  spacing: Style.space(14)
  RowLayout {
    width: root.width - root.padding * 2
    VitalsAction { id: backButton; iconName: "arrow-left"; foreground: root.secondary; tooltipText: root.tr("Back"); onClicked: root.back() }
    Text { text: root.tr("Settings"); color: root.secondary; font.family: "sans-serif"; font.pixelSize: Style.space(13); Layout.fillWidth: true }
    Text { visible: root.saving; text: root.tr("Saving…"); color: root.secondary; font.family: "sans-serif"; font.pixelSize: Style.space(11) }
  }
  Text {
    width: root.width - root.padding * 2
    visible: root.error !== ""
    text: root.error
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
    color: Color.urgent
    font.family: "sans-serif"
    font.pixelSize: Style.space(12)
    Accessible.role: Accessible.AlertMessage
  }
  Repeater {
    model: Preferences.fields
    Column {
      id: fieldRow
      required property var modelData
      required property int index
      readonly property var current: Preferences.value(root.settings, modelData.key)
      readonly property bool heading: !!modelData.group && (index === 0 || Preferences.fields[index - 1].group !== modelData.group)
      width: root.width - root.padding * 2
      spacing: Style.space(12)
      Column {
        width: parent.width
        visible: fieldRow.heading
        spacing: Style.space(16)
        Rectangle { width: parent.width; height: 1; color: Qt.alpha(Color.popups.text, 0.18) }
        Column {
          width: parent.width
          spacing: Style.space(5)
          Text { text: root.tr(fieldRow.modelData.group || ""); color: Color.popups.text; font.family: "sans-serif"; font.pixelSize: Style.space(12); font.weight: Font.DemiBold; width: parent.width; wrapMode: Text.WordWrap }
          Text { visible: text !== ""; text: root.tr(fieldRow.modelData.description || ""); color: root.secondary; font.family: "sans-serif"; font.pixelSize: Style.space(11); width: parent.width; wrapMode: Text.WordWrap }
        }
      }
      VitalsDropdown {
        width: parent.width
        visible: fieldRow.modelData.type === "enum"
        label: root.tr(fieldRow.modelData.label)
        fontFamily: "sans-serif"
        value: String(fieldRow.current)
        options: (fieldRow.modelData.options || []).map(function(v) { return {value: v, label: root.tr(Preferences.optionLabel(v))} })
        onChanged: function(value) { root.save(fieldRow.modelData.key, value) }
      }
      RowLayout {
        width: parent.width
        visible: fieldRow.modelData.type === "integer"
        spacing: Style.space(10)
        Text { Layout.fillWidth: true; text: root.tr(fieldRow.modelData.label); color: Color.popups.text; font.family: "sans-serif"; font.pixelSize: Style.space(13) }
        Controls.TextField {
          id: input
          Layout.preferredWidth: Style.space(68)
          implicitHeight: Style.space(34)
          text: String(fieldRow.current)
          selectByMouse: true
          color: Color.popups.text
          font.family: "sans-serif"
          font.pixelSize: Style.space(12)
          horizontalAlignment: Text.AlignRight
          padding: Style.space(7)
          Accessible.name: root.tr(fieldRow.modelData.group || "") + " " + root.tr(fieldRow.modelData.label)
          background: Rectangle { radius: Style.space(7); color: Qt.alpha(Color.popups.text, 0.055); border.width: input.activeFocus ? 1 : 0; border.color: Color.accent }
          onTextEdited: root.clearError()
          onEditingFinished: {
            if (!fieldRow.visible || fieldRow.modelData.type !== "integer") return
            var next = text.trim() === "" ? NaN : Number(text)
            if (next !== fieldRow.current) root.save(fieldRow.modelData.key, next)
          }
          Keys.onEscapePressed: { text = String(fieldRow.current); root.back() }
          HoverHandler { id: numberHover }
          PanelToolTip { visible: numberHover.hovered || input.activeFocus; text: fieldRow.modelData.min + "–" + fieldRow.modelData.max; fontFamily: "sans-serif" }
        }
        Text { Layout.preferredWidth: Style.space(20); text: fieldRow.modelData.unit || ""; color: root.secondary; font.family: "sans-serif"; font.pixelSize: Style.space(12) }
      }
    }
  }
}
