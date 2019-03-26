<?php
    $curl = curl_init();
    $url = "https://global.xirsys.net/_turn/".$_POST["channel"];
    curl_setopt_array( $curl, array (
        CURLOPT_URL => $url,
        CURLOPT_USERPWD => "ggsato:14f79f12-4161-11e9-b010-0242ac110003",
        CURLOPT_HTTPAUTH => CURLAUTH_BASIC,
        CURLOPT_CUSTOMREQUEST => "PUT",
        CURLOPT_RETURNTRANSFER => 1
    ));
    $resp = curl_exec($curl);
    print $resp;
    curl_close($curl);
?>