import QtQuick
import qs.Commons as Commons
import "../kit"
import ".." as Vitals

// Bar widget states for the foamy-kit harness:
//   render.py <plugin> --preview preview/BarPreview.qml
// Each row is the real VitalsBarLabel on a bar-coloured strip, coloured by a
// real VitalsReadings with the row's settings.
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["bar"]
  property string previewState: "bar"
  readonly property Item popup: popup
  readonly property var rows: [
    { label: "Normal", text: "CPU 23%", warning: false, theme: true },
    { label: "Adaptive: RAM is busiest", text: "RAM 64%", warning: false, theme: true },
    { label: "Warning, theme warning color", text: "CPU 97%", warning: true, theme: true },
    { label: "Warning, theme red", text: "CPU 97%", warning: true, theme: false },
    { label: "No reading", text: "GPU --", warning: false, theme: true }
  ]

  FoamyPopup {
    id: popup
    anchorItem: preview.anchorItem
    bar: preview.bar
    open: true
    body: Component {
      Column {
        width: FoamyStyle.popupWidth
        padding: FoamyStyle.padding
        spacing: FoamyStyle.gap
        Repeater {
          model: preview.rows
          Row {
            required property var modelData
            spacing: FoamyStyle.gapLarge
            Text {
              width: 220
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.label
              color: FoamyPalette.text
              font.family: FoamyStyle.fontFamily
              font.pixelSize: FoamyStyle.font.body
            }
            Rectangle {
              width: 120; height: 30
              radius: FoamyStyle.radiusSmall
              color: Commons.Color.bar.background
              Vitals.VitalsReadings { id: readings; settings: ({ useThemeWarningColors: modelData.theme }); stats: ({}) }
              Vitals.VitalsBarLabel {
                anchors.centerIn: parent
                text: modelData.text
                warning: modelData.warning
                foreground: Commons.Color.bar.text
                warningColor: readings.warningColor(Commons.Color.bar.background, Commons.Color.urgent)
              }
            }
          }
        }
      }
    }
  }
}
